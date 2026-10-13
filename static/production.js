'use strict';

// Production jobs: pick a voiced variant, keep a voice library and generate timed voiceovers.
window.CaseProduction = (() => {
  const statusLabels = {draft:'待配音',voiced:'已配音',timeline:'已建时间线',editing:'剪辑中',awaiting_review:'待人工审查',approved:'已完成',needs_material:'待补素材',needs_repair:'待修正',verification_failed:'核验未完成',unverified:'旧记录待核验',completed:'旧记录待核验',failed:'剪辑失败',cancelled:'剪辑已停止',interrupted:'剪辑已中断'};
  const frameRates = ['23.976','24','25','29.97','30','50','59.94','60'];
  const editModes = {explain:'解释画面逻辑（默认）',sync:'音画同步',montage:'主题蒙太奇'};
  const editModels = {'claude-default':'Claude 客户端默认','claude-opus':'Claude Opus','claude-sonnet':'Claude Sonnet'};
  const editModelNames = new Map([...Object.entries(editModels),['codex-default','Codex 客户端默认']]);
  const editProvider = model => String(model || '').startsWith('claude-') ? 'claude' : 'codex';
  const editClient = provider => provider === 'codex' ? 'Codex 客户端' : 'Claude Code 客户端';
  const editModelLabel = model => editModelNames.get(model || 'codex-default') || model;
  const editLabels = {running:'运行中',completed:'已完成',awaiting_review:'待人工审查',approved:'已完成',needs_material:'待补素材',needs_repair:'待修正',verification_failed:'核验未完成',unverified:'旧记录待核验',failed:'失败',cancelled:'已停止',interrupted:'已中断'};
  const skillDelivery = edit => edit?.completion_policy === 'skill_delivery';
  const editState = edit => edit.status === 'completed' ? (skillDelivery(edit) ? (edit.report?.saved !== true ? 'needs_repair' : edit.report?.gaps?.length ? 'needs_material' : edit.approval ? 'approved' : 'awaiting_review') : 'unverified') : edit.status === 'awaiting_review' && edit.approval ? 'approved' : edit.status;
  const manualScope = ['画面内容与口播是否匹配','镜头时长例外与视觉连贯性','字幕、声音听审和成片导出'];
  // Only the latest attempt on a timeline of the selected voiceover decides the job, so only it can be completed.
  const currentEdit = (job,edit) => {
    const timeline = (job.timelines || []).find(item => item.unique_id === edit.timeline_uid);
    const latest = [...(job.edits || [])].reverse().find(item => item.timeline_uid === edit.timeline_uid);
    return Boolean(timeline && job.selected_take && timeline.take_id === job.selected_take && latest?.id === edit.id);
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
  const view = {sequence:0,modal:null,pending:new Map(),choice:{},showArchived:false,type:'all',status:'all',batch:false,selected:new Set()};
  const queueTypes = {all:'全部',video:'视频',carousel:'图文'};
  const typeStorageKey = product => `case-workbench:production-type:${window.WorkbenchShell?.normalizeProduct?.(product) || 'all'}`;
  const storedType = product => {try {const value = localStorage.getItem(typeStorageKey(product));return Object.hasOwn(queueTypes,value) ? value : 'all';} catch {return 'all';}};
  const rememberType = (product,value) => {try {localStorage.setItem(typeStorageKey(product),value);} catch {}};
  const queueStatuses = {all:'全部状态',in_progress:'制作中',needs_attention:'需处理',awaiting_review:'待核对 / 审查',ready_to_package:'待整理成品包',ready:'可领取',approved:'已人工审查',published:'已登记发布'};
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
    $$('[data-activity-source]',$('#content')).forEach(card => {
      if ($('.task-activity-logs',card)) return;
      const host = document.createElement('div');host.className = 'task-activity-logs';card.append(host);
      mountActivityHost(host,{owner_id:ownerId,source_id:card.dataset.activitySource});
    });
    if (!$('#production-activity-logs')) {
      const host = document.createElement('div');host.id = 'production-activity-logs';host.className = 'task-activity-logs';
      $('.page-heading',$('#content'))?.after(host);
      const requested = new URLSearchParams(pageHash().split('?').slice(1).join('?')).get('run');
      const hasCard = requested && $$('[data-activity-source]',$('#content')).some(node => node.dataset.activitySource === requested);
      mountActivityHost(host,{owner_id:ownerId,...(requested && !hasCard ? {source_id:requested} : {})});
    }
  }
  function applyActivityTarget(id, hash) {
    if (view.activityTarget === hash) return;
    const params = new URLSearchParams(hash.split('?').slice(1).join('?')), run = params.get('run');
    const step = activitySteps[params.get('step')] || params.get('step');
    if (step) (view.choice[id] ||= {}).step = step;
    view.pendingActivityTarget = {hash,run,step};
  }
  function focusActivityTarget() {
    const target = view.pendingActivityTarget;if (!target) return;
    view.pendingActivityTarget = null;view.activityTarget = target.hash;
    if (!target.run && !target.step) return;
    const card = $$('[data-activity-source]',$('#content')).find(node => node.dataset.activitySource === target.run) || $$('[data-generation-result]',$('#content')).find(node => node.dataset.generationResult === target.run);
    const panel = card?.closest('[data-step-panel]');
    if (panel?.hidden) $$('[data-step]',$('.production-steps')).find(button => button.dataset.step === panel.dataset.stepPanel)?.click();
    if (card?.closest('.image-version-others')?.hidden) setImageVersionsExpanded(card.closest('.image-version-stack'),true);
    const node = card || (target.run ? $('#production-activity-logs') : null) || $$('[data-step-panel]',$('#content')).find(node => !node.hidden);
    if (node) {if (!card && target.run) {const details = $('details',node);if (details) details.open = true;}node.tabIndex = -1;node.focus({preventScroll:true});node.scrollIntoView({block:'start'});}
  }
  const fail = error => error?.message || '操作暂未完成';
  const definitive = error => Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
  const jobLink = job => url(`#production/${encodeURIComponent(job.id)}`,job.product);
  const seconds = value => value === null || value === undefined ? '—' : `${Number(value).toFixed(2)} 秒`;
  const voiceLabel = voice => [voice.note,voice.name].filter(Boolean).join(' · ') || voice.voice_id;
  const audioURL = (job,take,kind,download = false) => `/api/production/audio/${encodeURIComponent(job)}/${encodeURIComponent(take)}/${kind}${download ? '?download=1' : ''}`;
  const tabs = () => `<nav class="scripts-tabs" aria-label="内容创作"><a href="${esc(url('#ai'))}">协作任务</a><a href="${esc(url('#scripts'))}">脚本版本</a><a href="${esc(url('#production'))}" aria-current="page">制作单</a></nav>`;
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

  function modalOpen() {return !!view.modal;}
  function closeModal(force = false) {
    const modal = view.modal;if (!modal || (modal.busy && !force)) return;
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
    return `<div class="production-table-wrap"><table class="production-table"><thead><tr><th>#</th><th>口播</th><th>中文</th><th>画面要证明什么</th><th>素材标签</th><th>证据</th>${groups ? '<th>配音时间</th>' : ''}</tr></thead><tbody>${lines.map(line => {
      const group = timing.get(line.number);
      return `<tr><td>${line.number}</td><td class="spoken">${esc(line.spoken)}</td><td>${esc(line.zh || '—')}</td><td>${esc(line.proof || '待补')}</td><td>${esc(line.tags || '—')}</td><td>${esc(line.evidence || '—')}</td>${groups ? `<td class="timing">${group && group.start !== null ? `${Number(group.start).toFixed(2)}–${Number(group.end).toFixed(2)}` : '—'}</td>` : ''}</tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function problemsMarkup(problems) {
    return problems?.length ? `<div class="production-problems"><strong>需要核对</strong><ul>${problems.map(item => `<li>${esc(item)}</li>`).join('')}</ul></div>` : '';
  }
  function recoverySection(job) {
    const operations = job.recovery_operations || [], busy = job.voicing || (job.edits || []).some(edit => edit.status === 'running');
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
        await api(`/api/production/jobs/${encodeURIComponent(job.id)}/recover`,{method:'POST',body:{request_id:button.dataset.recoverOperation,kind:button.dataset.recoverKind}});
        toast('原请求的结果已接续登记。');await route(true);
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

  function captureListState() {
    if (!$('#production-settings-open') || !$('.production-main')) return null;
    return {product:view.listProduct,showArchived:!!$('#production-archived')?.checked,type:$('#production-type [aria-pressed="true"]')?.dataset.type || 'all',status:$('#production-status')?.value || 'all'};
  }

  async function route(keepScroll = false, refreshStatus = false) {
    const hash = pageHash(), seq = ++view.sequence, tail = hash.replace(/^#production\/?/,'').split('?')[0];
    if (!tail) {
      const mounted = keepScroll && view.listProduct === currentProduct() ? captureListState() : null;
      const saved = window.WorkbenchShell?.listState?.('production',currentProduct());
      view.showArchived = (saved || mounted)?.showArchived === true;
      view.type = Object.hasOwn(queueTypes,(saved || mounted)?.type) ? (saved || mounted).type : storedType(currentProduct());
      view.status = Object.hasOwn(queueStatuses,(saved || mounted)?.status) ? (saved || mounted).status : 'all';
    }
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
      } else {
        const data = await api(`/api/production?product=${encodeURIComponent(currentProduct())}`);
        if (seq !== view.sequence || hash !== pageHash()) return;
        renderList(data);
      }
    } catch (error) {
      if (seq !== view.sequence || hash !== pageHash()) return;
      if (tail && error.status === 404) {$('#content').innerHTML = `<div class="empty"><h3>这张制作单已不存在</h3><p>可能已被删除，或属于其他产品。</p><a class="secondary" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#production'))}">返回制作清单</a></div>`;return;}
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

  function queueState(item,kind,delivery,deliveryState) {
    if (item.archived) return {key:'archived',label:'已归档',action:'查看制作单',note:'恢复后可继续制作'};
    if (kind === 'video') {
      if (item.voicing) return {key:'in_progress',label:'配音中',action:'查看进度',step:'voice'};
      if (item.recovery_operations?.length) return {key:'needs_attention',label:'结果待恢复',action:'查看并恢复'};
      if (['awaiting_review','approved'].includes(item.status)) {
        // A result from another timeline cannot override the job's latest state.
        const edit = (item.edits || []).find(edit => edit.id === delivery?.edit_id);
        const reviewable = delivery?.status === 'awaiting_review' && edit && currentEdit(item,edit) &&
          ((skillDelivery(edit) && edit.status === 'completed' && edit.report?.saved === true && !edit.report?.gaps?.length) ||
          (edit.status === 'awaiting_review' && edit.validation?.status === 'awaiting_review'));
        if (reviewable && item.status === 'approved' && delivery.approved_at) return {key:'approved',label:'已人工审查',action:'查看制作单',step:'edit',note:'人工审查已确认；成片导出在达芬奇完成',delivery:true};
        if (reviewable && item.status === 'awaiting_review' && !delivery.approved_at) return {key:'awaiting_review',label:'待人工审查',action:'去审查',step:'edit',note:skillDelivery(edit) && !edit.validation ? '按技能保存交付 · 工程核验未执行' : '工程核验通过 · 待人工审查',delivery:true};
        return {key:'needs_attention',label:deliveryState.loading ? '审查状态核对中' : '审查状态待确认',action:'查看制作单',step:'edit',note:deliveryState.error || '进入制作单核对当前工程与审查记录'};
      }
      const problem = ['needs_material','needs_repair','verification_failed','unverified','completed','failed','cancelled','interrupted'].includes(item.status);
      return {key:problem ? 'needs_attention' : 'in_progress',label:statusLabels[item.status] || '待处理',action:problem ? '查看并处理' : item.status === 'editing' ? '查看进度' : '继续制作',step:['draft','voiced'].includes(item.status) ? 'voice' : 'edit'};
    }
    if ((item.generations || []).some(run => run.status === 'running') || (item.searches || []).some(run => run.status === 'running')) return {key:'in_progress',label:'制作中',action:'查看进度',step:(item.generations || []).some(run => run.status === 'running') ? 'images' : 'pick'};
    if (delivery?.ready_to_package === true) {
      if (delivery.delivery_available === true && delivery.current_delivery_id && delivery.delivery_id === delivery.current_delivery_id) {
        return {key:delivery.published ? 'published' : 'ready',label:delivery.published ? '已登记发布' : '可领取',action:delivery.published ? '查看发布记录' : '领取成品',step:'delivery',note:delivery.published ? '当前成品包已登记发布' : '当前图文已确认 · 成品包完整可用',delivery:true};
      }
      return {key:'ready_to_package',label:'待整理成品包',action:'整理成品包',step:'delivery',note:delivery.delivery_error || '当前图文已确认，整理后可领取',delivery:true};
    }
    if (item.status === 'approved' || item.status === 'published') return {key:'needs_attention',label:deliveryState.loading ? '领取状态核对中' : '领取状态待确认',action:'查看制作单',step:'delivery',note:deliveryState.error || '请核对当前图片与成品包'};
    if (item.status === 'reviewing') return {key:'awaiting_review',label:'成图待确认',action:'选择图片',step:'images',note:delivery?.delivery_id ? '有历史成品包；当前图片待选用' : ''};
    return {key:'in_progress',label:carouselLabel(item),action:'继续制作',step:['ready','picked'].includes(item.status) ? 'images' : 'pick',note:delivery?.delivery_id ? '历史成品包可在制作单中查看' : ''};
  }

  function mountQueue(data,product,sequence) {
    const node = $('#production-queue'), statusNode = $('#production-queue-status');
    const deliveryState = {loading:true,items:[],error:''};
    let readSequence = 0;
    const paint = () => {
      if (!node.isConnected) return;
      const deliveries = new Map(deliveryState.items.map(item => [`${item.kind}:${item.id}`,item]));
      const rows = [...(data.jobs || []).map(item => ({item,kind:'video'})),...(data.carousels || []).map(item => ({item,kind:'carousel'}))]
        .filter(({item}) => view.showArchived || !item.archived)
        .map(row => ({...row,state:queueState(row.item,row.kind,deliveries.get(`${row.kind}:${row.item.id}`),deliveryState)}))
        .sort((a,b) => String(b.item.updated_at || '').localeCompare(String(a.item.updated_at || '')) || String(a.item.id).localeCompare(String(b.item.id)));
      const visible = rows.filter(row => (view.type === 'all' || row.kind === view.type) && (view.status === 'all' || row.state.key === view.status));
      $('#production-queue-count').textContent = `${visible.length} / ${rows.length} 张`;
      // Filters and batch actions only mean something once there are orders to act on.
      const filters = $('.production-queue-filters');if (filters) filters.hidden = !rows.length;
      statusNode.hidden = !deliveryState.loading && !deliveryState.error;
      statusNode.innerHTML = deliveryState.error ? `<span>暂时无法核对审查与领取状态：${esc(deliveryState.error)}</span><button type="button" class="subtle" data-delivery-reload>重新读取</button>` : deliveryState.loading ? '<span>正在核对审查与领取状态…</span>' : '';
      $('[data-delivery-reload]',statusNode)?.addEventListener('click',load);
      node.innerHTML = visible.length ? visible.map(({item,kind,state}) => `<a class="script-card production-card production-queue-row${view.batch ? ' selecting' : ''}${view.batch && view.selected.has(item.id) ? ' selected' : ''}" href="${esc(jobLink(item))}" ${view.batch ? `role="checkbox" aria-checked="${view.selected.has(item.id)}"` : ''} data-production-id="${esc(item.id)}" data-production-kind="${kind}" data-production-state="${state.key}" ${state.step ? `data-production-step="${state.step}"` : ''} ${state.delivery ? `data-delivery-kind="${kind}" data-delivery-item="${esc(item.id)}"` : ''}>${view.batch ? '<span class="production-pick" aria-hidden="true"></span>' : ''}<span class="script-row-main"><span class="production-row-title"><span class="production-kind">${kind === 'video' ? '视频' : '图文'}</span><strong>${esc(item.title || '未命名制作单')}</strong></span><small>${esc(item.product)} · ${esc(item.direction_id || '未指定方向')}${kind === 'video' ? ` · ${(item.lines || []).length} 个意群` : ` · ${(item.slides || []).length} 张图片`} · ${esc(readableDate(item.updated_at))}</small>${state.note ? `<small class="production-row-note">${esc(state.note)}</small>` : ''}</span><span class="production-row-next"><span class="script-status ${['ready','published','approved'].includes(state.key) ? 'published' : ''}">${esc(state.label)}</span><span class="production-next-action">${esc(state.action)}${icon('arrow')}</span></span></a>`).join('') : `<div class="empty"><h3>${rows.length ? '没有符合筛选的制作单' : '还没有制作单'}</h3><p>${rows.length ? '调整类型或状态筛选，查看其他制作单。' : '在脚本详情中选择视频或图文，开始制作。'}</p>${rows.length ? '<button type="button" class="secondary" data-queue-reset>查看全部</button>' : `<a class="secondary" href="${esc(url('#scripts',product))}">查看脚本版本</a>`}</div>`;
      $('[data-queue-reset]',node)?.addEventListener('click',() => {view.type = view.status = 'all';rememberType(product,'all');syncType();$('#production-status').value = 'all';paint();});
      paintBatch(visible,rows);
    };
    const syncType = () => $$('.production-type-choice',$('#production-type')).forEach(button => button.setAttribute('aria-pressed',String(button.dataset.type === view.type)));
    const orderRef = ({item,kind}) => ({kind:kind === 'video' ? 'job' : 'carousel',id:item.id});
    const paintBatch = (visible,rows) => {
      const bar = $('#production-batchbar'), toggle = $('#production-batch-toggle');
      if (!bar) return;
      toggle.setAttribute('aria-pressed',String(view.batch));toggle.textContent = view.batch ? '退出批量管理' : '批量管理';
      bar.hidden = !view.batch;
      if (!view.batch) return;
      const known = new Set(rows.map(row => row.item.id));
      for (const id of [...view.selected]) if (!known.has(id)) view.selected.delete(id);
      const chosen = visible.filter(row => view.selected.has(row.item.id));
      const live = chosen.filter(row => !row.item.archived).length, archived = chosen.length - live;
      bar.innerHTML = `<span class="production-batch-count" role="status">已选 ${chosen.length} 张${chosen.length && archived ? `（其中已归档 ${archived}）` : ''}</span><button type="button" class="subtle" data-batch="all">${chosen.length === visible.length && visible.length ? '取消全选' : `全选当前 ${visible.length} 张`}</button><button type="button" class="secondary compact" data-batch="archive" ${live ? '' : 'disabled'}>归档 ${live || ''}</button><button type="button" class="secondary compact" data-batch="restore" ${archived ? '' : 'disabled'}>恢复 ${archived || ''}</button><button type="button" class="secondary compact danger" data-batch="delete" ${chosen.length ? '' : 'disabled'}>永久删除 ${chosen.length || ''}</button><small>永久删除不需要先归档；有任务在运行的制作单会被跳过并说明原因。</small>`;
      $$('[data-batch]',bar).forEach(button => button.onclick = () => {
        const action = button.dataset.batch;
        if (action === 'all') {
          if (chosen.length === visible.length) view.selected.clear();
          else visible.forEach(row => view.selected.add(row.item.id));
          paint();return;
        }
        const targets = chosen.filter(row => action === 'delete' || (action === 'archive' ? !row.item.archived : row.item.archived)).map(orderRef);
        void runBatch(action,targets);
      });
    };
    const load = async () => {
      const request = ++readSequence;deliveryState.loading = true;deliveryState.error = '';paint();
      try {
        const result = await api(`/api/production/deliveries?product=${encodeURIComponent(product)}`);
        if (!node.isConnected || sequence !== view.sequence || request !== readSequence) return;
        deliveryState.items = result.items || [];
      } catch (error) {
        if (!node.isConnected || sequence !== view.sequence || request !== readSequence) return;
        deliveryState.items = [];deliveryState.error = fail(error);
      }
      deliveryState.loading = false;paint();
    };
    syncType();
    $$('.production-type-choice',$('#production-type')).forEach(button => button.onclick = () => {view.type = button.dataset.type;rememberType(product,view.type);syncType();paint();});
    const statusSelect = $('#production-status');statusSelect.value = view.status;statusSelect.onchange = () => {view.status = statusSelect.value;paint();};
    $('#production-batch-toggle').onclick = () => {view.batch = !view.batch;if (!view.batch) view.selected.clear();paint();};
    $('#production-archived')?.addEventListener('change',event => {view.showArchived = event.target.checked;paint();});
    node.addEventListener('click',event => {
      if (view.batch) {
        const pick = event.target.closest('[data-production-id]');
        if (pick) {
          event.preventDefault();
          const id = pick.dataset.productionId;
          if (view.selected.has(id)) view.selected.delete(id);else view.selected.add(id);
          paint();
        }
        return;
      }
      const row = event.target.closest('[data-production-step]');
      if (row && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) (view.choice[row.dataset.productionId] ||= {}).step = row.dataset.productionStep;
    });
    void load();
  }

  function renderList(data) {
    const product = currentProduct();
    view.listProduct = product;
    const archivedCount = [...(data.jobs || []), ...(data.carousels || [])].filter(item => item.archived).length;
    const voices = data.voices || [];
    $('#content').innerHTML = `<div class="page-heading"><div><h1>内容创作</h1><p>管理视频与图文制作单，继续配音、剪辑或出图，核对并领取成品。</p></div><div class="scripts-start-actions"><button type="button" class="secondary" id="production-refresh">${icon('refresh')}刷新</button><button type="button" class="secondary" id="production-settings-open" aria-haspopup="dialog" aria-controls="production-settings">${icon('folder')}制作设置</button></div></div>${tabs()}<div class="production-service-warning" id="production-service-warning" role="status" hidden></div><div class="production-main"><section aria-labelledby="production-queue-title"><div class="section-title-row production-list-head"><h2 id="production-queue-title">制作清单 <small id="production-queue-count"></small></h2>${archivedCount ? `<label class="production-toggle"><input type="checkbox" id="production-archived" ${view.showArchived ? 'checked' : ''}>显示已归档（${archivedCount}）</label>` : ''}</div><div class="production-queue-filters"><div class="production-type-field"><span id="production-type-label">类型</span><div class="production-type-group" id="production-type" role="group" aria-labelledby="production-type-label">${Object.entries(queueTypes).map(([key,label]) => `<button type="button" class="production-type-choice" data-type="${key}" aria-pressed="false">${label}</button>`).join('')}</div></div><label>状态<select id="production-status">${Object.entries(queueStatuses).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select></label><button type="button" class="secondary compact" id="production-batch-toggle" aria-pressed="false">批量管理</button></div><div class="production-batchbar" id="production-batchbar" hidden></div><div class="production-queue-status" id="production-queue-status" role="status"></div><div id="production-queue" class="script-rows"></div></section></div><dialog id="production-settings" class="rev-drawer production-drawer" aria-labelledby="production-settings-title"><div class="rev-drawer-head"><h2 id="production-settings-title">制作设置</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭制作设置">${icon('close')}</button></div><div class="rev-drawer-body"><section class="production-setting"><h2>配音服务</h2><div class="production-eleven" id="production-eleven"></div></section><section class="production-setting"><h2>音色库</h2><p class="context-note">生成过的音色会自动留在这里；备注写清是谁的声音、适合什么稿。</p><ul class="voice-list">${voices.length ? voices.map(voice => `<li class="voice-row ${voice.disabled ? 'disabled' : ''}"><div><strong>${esc(voiceLabel(voice))}</strong>${product && data.defaults?.[window.WorkbenchShell.normalizeProduct(product)] === voice.voice_id ? '<span class="script-status published">默认</span>' : ''}${voice.disabled ? '<span class="script-status">已停用</span>' : ''}<code>${esc(voice.voice_id)}</code><small>使用 ${voice.use_count || 0} 次${voice.last_used_at ? ` · 最近 ${esc(readableDate(voice.last_used_at))}` : ''}</small></div><div class="voice-actions"><button type="button" class="subtle" data-voice-note="${esc(voice.voice_id)}">备注</button>${product && !voice.disabled ? `<button type="button" class="subtle" data-voice-default="${esc(voice.voice_id)}">设为默认</button>` : ''}<button type="button" class="subtle" data-voice-toggle="${esc(voice.voice_id)}">${voice.disabled ? '启用' : '停用'}</button></div></li>`).join('') : '<li class="context-note">还没有音色。</li>'}</ul><form id="voice-add" class="inline-form"><label class="field"><span>音色 ID</span><input name="voice_id" required pattern="[A-Za-z0-9]{10,64}" autocomplete="off"></label><label class="field"><span>备注</span><input name="note" maxlength="500" placeholder="例如：Zack 男声，适合测评稿"></label><button type="submit" class="secondary">核对并加入</button><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="production-setting"><h2>读法表${product ? ` · ${esc(product)}` : ''}</h2>${product ? `<p class="context-note">只改送去配音的文字，稿件原文不变。按整词、区分大小写替换。</p><ul class="reading-list">${(data.readings || []).map(item => `<li><span><code>${esc(item.written)}</code> 读作 <strong>${esc(item.spoken)}</strong></span><button type="button" class="subtle" data-reading-remove="${esc(item.id)}">删除</button></li>`).join('') || '<li class="context-note">还没有登记读法。</li>'}</ul><form id="reading-add" class="inline-form"><label class="field"><span>稿中写法</span><input name="written" required maxlength="100" placeholder="例如 202H"></label><label class="field"><span>配音读法</span><input name="spoken" required maxlength="200" placeholder="例如 two oh two H"></label><button type="submit" class="secondary">添加读法</button><p class="ai-error" data-error role="alert" hidden></p></form>` : '<p class="context-note">选择具体产品后维护读法。</p>'}</section>${resolveSettings(data,product)}</div></dialog>`;
    elevenStatus($('#production-eleven'));
    mountQueue(data,product,view.sequence);
    bindResolveSettings(data,product);
    // Saving a setting re-reads the page; the drawer stays open while you keep editing.
    const drawer = $('#production-settings');
    $('#production-settings-open').onclick = () => {view.settingsOpen = true;drawer.showModal();};
    $('#production-refresh').onclick = () => route(true,true);
    drawer.addEventListener('click',event => {if (event.target === drawer || event.target.closest('[data-drawer-close]')) drawer.close();});
    drawer.addEventListener('close',() => {view.settingsOpen = false;});
    if (view.settingsOpen) drawer.showModal();
    const voiceForm = $('#voice-add');
    voiceForm.onsubmit = async event => {
      event.preventDefault();const values = Object.fromEntries(new FormData(voiceForm));setError(voiceForm,'');
      const button = $('button[type="submit"]',voiceForm);button.disabled = true;
      try {await send('voice-add','/api/production/voices',{revision:data.revision,operation:'save',voice:{voice_id:values.voice_id.trim(),note:values.note.trim()}});toast('音色已核对并加入音色库。');await route(true);}
      catch (error) {setError(voiceForm,fail(error));if (error.status === 409) data.revision = (await api('/api/production')).revision;}
      finally {button.disabled = false;}
    };
    $$('[data-voice-note]').forEach(button => button.onclick = () => {
      const voice = voices.find(item => item.voice_id === button.dataset.voiceNote);
      openModal('音色备注',`<p class="context-note">${esc(voice.name || '未取得名称')} · <code>${esc(voice.voice_id)}</code></p><label class="field"><span>备注</span><input name="note" maxlength="500" value="${esc(voice.note || '')}"></label>`,'保存备注',async modal => {
        const note = new FormData(modal.form).get('note').trim();
        await once(modal,'note','/api/production/voices',async () => ({revision:(await api('/api/production')).revision,operation:'save',voice:{voice_id:voice.voice_id,note,model_id:voice.model_id,settings:voice.settings}}));
        closeModal(true);toast('备注已保存。');await route(true);
      });
    });
    const voiceAction = async (operation,voiceId,message) => {
      try {await send(`voice-${operation}-${voiceId}`,'/api/production/voices',{revision:data.revision,operation,product,voice:{voice_id:voiceId}});toast(message);await route(true);}
      catch (error) {toast(fail(error),true);if (error.status === 409) await route(true);}
    };
    $$('[data-voice-default]').forEach(button => button.onclick = () => voiceAction('default',button.dataset.voiceDefault,`已设为 ${product} 的默认音色。`));
    $$('[data-voice-toggle]').forEach(button => {
      const voice = voices.find(item => item.voice_id === button.dataset.voiceToggle);
      button.onclick = () => voiceAction(voice.disabled ? 'enable' : 'disable',voice.voice_id,voice.disabled ? '音色已启用。' : '音色已停用，历史配音不受影响。');
    });
    const readingForm = $('#reading-add');
    if (readingForm) readingForm.onsubmit = async event => {
      event.preventDefault();const values = Object.fromEntries(new FormData(readingForm));setError(readingForm,'');
      try {await send('reading-add','/api/production/readings',{revision:data.revision,reading:{product,written:values.written.trim(),spoken:values.spoken.trim()}});toast('读法已添加。');await route(true);}
      catch (error) {setError(readingForm,fail(error));if (error.status === 409 && /已更新/.test(fail(error))) data.revision = (await api('/api/production')).revision;}
    };
    $$('[data-reading-remove]').forEach(button => button.onclick = async () => {
      const item = data.readings.find(entry => entry.id === button.dataset.readingRemove);
      try {await send(`reading-remove-${item.id}`,'/api/production/readings',{revision:data.revision,operation:'remove',reading:{id:item.id}});toast('读法已删除。');await route(true);}
      catch (error) {toast(fail(error),true);}
    });
  }

  function resolveSettings(data,product) {
    const settings = data.settings || {creators:[]}, config = data.product_settings;
    return `<section class="production-setting"><h2>达芬奇设置${product ? ` · ${esc(product)}` : ''}</h2><form id="resolve-settings" class="inline-form"><label class="field"><span>达人名（逗号分隔，作为时间线名开头）</span><input name="creators" value="${esc((settings.creators || []).join(', '))}" placeholder="例如 Zack, Lab" autocomplete="off"></label>${config ? `<label class="field"><span>达芬奇项目</span><input name="project" required maxlength="200" value="${esc(config.project)}"></label><div class="form-row"><label class="field"><span>时间线编号</span><input name="code" required maxlength="20" pattern="[A-Za-z0-9]+" value="${esc(config.code)}"></label><label class="field"><span>帧率</span><select name="fps">${frameRates.map(value => `<option ${value === config.fps ? 'selected' : ''}>${value}</option>`).join('')}</select></label></div><div class="form-row"><label class="field"><span>宽</span><input name="width" type="number" min="16" max="8192" required value="${esc(config.width)}"></label><label class="field"><span>高</span><input name="height" type="number" min="16" max="8192" required value="${esc(config.height)}"></label></div><p class="context-note">新时间线命名如 <code>${esc((settings.creators || [])[0] || '达人')}${today()}-${esc(config.code)}-序号</code>；配音放「${esc(config.audio_bin)}」素材夹，时间线放「${esc(config.timeline_bin)}」素材夹。工作台只在你已经打开的这个项目里建时间线，不会切换项目。</p>` : '<p class="context-note">选择具体产品后设置达芬奇项目和时间线编号。</p>'}<button type="submit" class="secondary">保存设置</button><p class="ai-error" data-error role="alert" hidden></p></form></section>`;
  }
  function bindResolveSettings(data,product) {
    const form = $('#resolve-settings'), config = data.product_settings;
    form.onsubmit = async event => {
      event.preventDefault();setError(form,'');
      const values = Object.fromEntries(new FormData(form));
      const settings = {creators:values.creators.split(/[,，\s]+/).filter(Boolean)};
      if (config) Object.assign(settings,{product,project:values.project.trim(),code:values.code.trim(),fps:values.fps,width:Number(values.width),height:Number(values.height),audio_bin:config.audio_bin,timeline_bin:config.timeline_bin});
      const button = $('button[type="submit"]',form);button.disabled = true;
      try {await send('resolve-settings','/api/production/settings',{revision:data.revision,settings});toast('达芬奇设置已保存。');await route(true);}
      catch (error) {setError(form,fail(error));if (error.status === 409 && /已更新/.test(fail(error))) data.revision = (await api('/api/production')).revision;}
      finally {button.disabled = false;}
    };
  }
  async function resolveStatus(node,config,request) {
    try {
      const status = await request;
      if (!node.isConnected) return;
      node.classList.toggle('problem',!status.ok || status.project !== config.project);
      node.textContent = !status.ok ? status.error
        : status.project !== config.project ? `达芬奇当前打开的是项目「${status.project}」。建时间线前请先在达芬奇里打开「${config.project}」，工作台不会替你切换项目。`
        : `达芬奇 ${status.version} 已打开项目「${status.project}」（${status.fps} fps，${status.width}×${status.height}）。`;
    } catch (error) {if (!node.isConnected) return;node.classList.add('problem');node.textContent = `暂时无法检查达芬奇：${fail(error)}`;}
  }
  function editRun(job,edit,historicalRestore = false) {
    const summary = edit.summary, report = edit.report || {}, live = edit.live || {}, state = editState(edit);
    const unverifiedDelivery = skillDelivery(edit) && !edit.validation;
    const requestedModel = edit.requested_model || edit.model || 'codex-default', provider = edit.provider || editProvider(requestedModel), client = edit.client_label || editClient(provider);
    const effectiveModel = live.effective_model || edit.effective_model, effectiveEffort = live.effective_effort || edit.effective_effort;
    const receipt = `${esc(client)} · ${effectiveModel ? `实际模型：${esc(editModelLabel(effectiveModel))}` : `申请模型：${esc(editModelLabel(requestedModel))} · 实际型号尚未回报`}${effectiveModel && effectiveModel !== requestedModel ? `（申请 ${esc(editModelLabel(requestedModel))}）` : ''}${effectiveEffort ? ` · 实际思考强度：${esc(effectiveEffort)}` : edit.effort ? ` · 申请思考强度：${esc(edit.effort)}` : ''} · ${provider === 'codex' ? '使用 Codex 客户端当前账户额度' : '使用 Claude 订阅额度'}`;
    const gaps = summary?.gaps || (Array.isArray(report.gaps) ? report.gaps.map(gap => ({...gap,name:gap.need || '缺素材'})) : []), uncovered = summary?.uncovered || [];
    const gapLocation = gap => Number.isFinite(gap.seconds) ? `第 ${gap.seconds} 秒` : `帧 [${gap.start}, ${gap.end})`;
    const facts = summary ? [`镜头 ${summary.shots} 个`,`配音 ${summary.voice_seconds} 秒`,`缺素材 ${gaps.length} 处`,
      uncovered.length ? `未覆盖 ${uncovered.length} 段` : '画面全部覆盖',
      summary.long_shots ? `超过通用参考 ${summary.frame_limit} 帧的镜头 ${summary.long_shots} 个（最长 ${summary.max_shot} 帧，需按产品规则人工判断）` : ''].filter(Boolean) : unverifiedDelivery ?
      [Number.isInteger(report.shots) ? `剪辑说明：镜头 ${report.shots} 个` : '',`已报缺素材 ${gaps.length} 处`].filter(Boolean) : [];
    const issues = (edit.validation?.issues || []).map(issue => typeof issue === 'string' ? issue : issue.message).filter(Boolean);
    const warnings = issues.length ? issues : summary ? [summary.voice_changed ? '配音轨和建时间线时不一致，请核对是否被改动。' : '',
      summary.group_markers !== summary.groups_expected ? `意群标记读回 ${summary.group_markers} 个，应有 ${summary.groups_expected} 个。` : ''].filter(Boolean) : [];
    if (edit.readback_error && !issues.length) warnings.push(`没能读回时间线：${edit.readback_error}`);
    const checked = edit.validation?.checked_at ? ` · 核验 ${esc(readableDate(edit.validation.checked_at))}` : '';
    const approval = state === 'approved' ? edit.approval : null;
    const explanation = approval ? `你已在 ${readableDate(approval.at)} 人工确认完成${approval.note ? `：${approval.note}` : '。'}${unverifiedDelivery ? '此记录仅表示人工审查已确认，工程核验未执行。' : '之后重新核验时镜头有变化，会自动撤销确认。'}`
      : state === 'awaiting_review' ? unverifiedDelivery ? '已按技能完成保存交付，待你在达芬奇人工审查；工程核验未执行，素材去重已归档停用。字幕、声音和导出由你处理。核对完点「确认完成」。' : `工程核验通过${edit.validation?.dedup_enabled === false ? '（素材去重未执行）' : ''}，待你在达芬奇人工审查；字幕、声音和导出由你处理。核对完点「确认完成」。`
      : state === 'unverified' ? '旧记录未保存完整核验依据，不能作为可审查成品；可读回现有工程重新核验。' : '';
    const busy = (job.edits || []).some(entry => entry.status === 'running');
    const missingBaseline = !edit.baseline && (edit.validation?.issues || []).some(issue => issue?.code === 'baseline_missing');
    const resultRecovery = skillDelivery(edit) && edit.result_pending;
    const actions = edit.status === 'running' ? `<button type="button" class="secondary" data-cancel-edit="${esc(edit.id)}">停止</button>`
      : `<div class="edit-actions">${missingBaseline ? `<button type="button" class="secondary" data-adopt-baseline="${esc(edit.id)}" ${busy || !historicalRestore ? 'disabled' : ''}>补齐核验依据</button>` : ''}${resultRecovery ? `<button type="button" class="secondary" data-recover-edit="${esc(edit.id)}" ${busy ? 'disabled' : ''}>恢复结果</button>` : `<button type="button" class="secondary" data-recheck-edit="${esc(edit.id)}" ${busy ? 'disabled' : ''}>重新核验</button>`}${approval ? `<button type="button" class="secondary" data-revoke-edit="${esc(edit.id)}" ${busy ? 'disabled' : ''}>撤销完成</button>`
        : state === 'awaiting_review' && currentEdit(job,edit) && !job.archived ? `<button type="button" class="primary" data-approve-edit="${esc(edit.id)}" ${busy ? 'disabled' : ''}>确认完成</button>` : ''}${job.archived ? '' : `<button type="button" class="subtle edit-delete" data-delete-edit="${esc(edit.id)}" ${busy && edit.status === 'running' ? 'disabled' : ''}>${icon('trash')}删除</button>`}</div>`;
    const evidence = edit.baseline_evidence || {};
    const adopted = edit.baseline_origin === 'adopted' ? `<p class="context-note">当前保护基准采纳于 ${esc(readableDate(edit.baseline_at))}：配音轨和意群标记以采纳时工程为准。这份基准不能证明更早的剪辑过程没有改动；镜头仍逐个核验。</p>`
      : edit.baseline_origin === 'historical_resume' ? `<div class="context-note historical-baseline"><p>历史核验依据来自 ${esc(readableDate(evidence.captured_at || edit.baseline_at))} 保存的「${esc(evidence.scope || 'Codex 接续剪辑前')}」快照，于 ${esc(readableDate(evidence.restored_at))} 恢复；来源资料与文件校验记录已保留。工程核验通过后仍待你人工审查，恢复依据不会自动确认完成。</p>${(evidence.unrecorded_fields || []).includes('voice.clip_enabled') ? '<p>历史快照未记录配音片段启用状态，按当前工程另行核对；画幅的宽和高来自当时项目设置。</p>' : ''}${evidence.sources?.length ? `<details><summary>查看历史依据来源</summary><ul class="edit-gaps">${evidence.sources.map(source => `<li><p>${esc(source.role)}：${esc(source.path)}</p><p><small>SHA-256：${esc(source.sha256)}</small></p></li>`).join('')}</ul></details>` : ''}</div>` : '';
    return `<article class="edit-card ${esc(state)}" data-edit-id="${esc(edit.id)}" data-activity-source="${esc(edit.id)}"><div class="take-head"><div><strong><span class="script-status ${['awaiting_review','approved'].includes(state) ? 'published' : ''}">${esc(editLabels[state] || state)}</span> ${esc(edit.timeline_name)}</strong><small>${receipt} · ${esc(editModes[edit.mode] || edit.mode)} · 开始 ${esc(readableDate(edit.started_at))}${edit.finished_at ? ` · 结束 ${esc(readableDate(edit.finished_at))}` : ''}${checked}</small>${edit.execution_status ? `<small>剪辑执行：${esc(editLabels[edit.execution_status] || edit.execution_status)}</small>` : ''}</div>${actions}</div>${explanation ? `<p class="edit-validation-note">${esc(explanation)}</p>` : ''}${missingBaseline && !historicalRestore ? '<p class="context-note">需要更新工作台后恢复历史核验依据。</p>' : ''}${adopted}${edit.status === 'running' ? `<p class="production-busy">${esc(live.progress || '正在剪辑…')}</p>${live.latest ? `<pre class="edit-live">${esc(live.latest)}</pre>` : ''}` : ''}${facts.length ? `<p class="edit-facts">${facts.map(esc).join(' · ')}</p>` : ''}${warnings.length ? `<ul class="take-warnings edit-validation-issues">${warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}${gaps.length ? `<h3>缺素材</h3><ul class="edit-gaps">${gaps.map(gap => `<li><strong>${esc(gapLocation(gap))} · ${esc(gap.name)}</strong>${gap.note ? `<p>${esc(gap.note)}</p>` : ''}</li>`).join('')}</ul>` : ''}${uncovered.length ? `<h3>没有画面、也没报缺素材的地方</h3><ul class="edit-gaps">${uncovered.map(item => `<li>第 ${item.seconds} 秒起，共 ${item.end - item.start} 帧</li>`).join('')}</ul>` : ''}${(report.questions || []).length ? `<h3>待你确认</h3><ul class="edit-gaps">${report.questions.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}${edit.error ? `<p class="ai-error">${esc(edit.error)}</p>` : ''}${edit.output ? `<details><summary>${esc(client)}的剪辑说明</summary><pre class="edit-output">${esc(edit.output)}</pre></details>` : ''}${edit.instruction ? `<p class="context-note">本次额外要求：${esc(edit.instruction)}</p>` : ''}</article>`;
  }
  function editSection(data,choice) {
    const job = data.job, timelines = [...(job.timelines || [])].reverse(), edits = [...(job.edits || [])].reverse();
    const running = edits.some(edit => edit.status === 'running');
    const olderIds = edits.slice(1).filter(edit => edit.status !== 'running').map(edit => edit.id);
    const form = !timelines.length ? '<p class="context-note">先建时间线，再让 AI 按意群标记去素材里选画面、拼成视频。</p>'
      : `<form id="edit-form"><div class="form-row"><label class="field"><span>目标时间线</span><select name="timeline">${timelines.map(item => `<option value="${esc(item.unique_id)}" ${item.unique_id === choice.edit_timeline ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}</select></label><label class="field"><span>选画面模式</span><select name="mode">${Object.entries(editModes).map(([key,label]) => `<option value="${key}" ${key === (choice.edit_mode || 'explain') ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div><fieldset class="material-picker"><legend>素材时间线</legend><div data-materials>正在读取达芬奇里的时间线…</div></fieldset><label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="3" maxlength="4000" placeholder="例如：开头用切纸结果；只剪前 4 个意群">${esc(choice.edit_instruction || '')}</textarea></label><p class="context-note"><span data-edit-default role="status">正在读取全局默认模型…</span> <a href="${esc(url('#settings'))}" data-edit-settings>修改全局设置</a> · <button type="button" class="subtle" data-edit-default-refresh>重新读取</button></p><div data-edit-override hidden><p class="context-note">当前全局默认暂不能用于 AI 剪辑，请为本次选择兼容模型，或修改全局设置。</p><div class="form-row"><label class="field"><span>本次剪辑模型</span><select name="model"><option value="">请选择兼容模型</option></select></label><label class="field"><span>本次思考强度</span><select name="effort"><option value="">客户端默认</option></select></label></div></div><p class="production-resolve" data-edit-provider-status role="status">正在检查剪辑客户端…</p><p class="context-note" data-edit-usage>AI 剪辑默认沿用全局模型与思考强度，支持 Codex 和 Claude。</p><div class="take-actions"><button type="submit" class="primary" disabled>开始 AI 剪辑</button><button type="button" class="secondary" data-copy-handoff>复制剪辑指令</button>${running ? '<span>已有剪辑在运行。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="edit-panel"><h2>AI 剪辑 <small>${edits.length} 次</small></h2>${form}<p class="context-note">剪辑按技能完成保存后交人工审查，默认不做剪后工程核验；素材去重已归档停用。</p>${edits.length ? '<p class="context-note">按需点击「重新核验」可读回并保存现有工程，素材去重仍停用。请先结束正在进行的剪辑。</p>' : ''}<div id="edit-runs">${edits.slice(0,1).map(edit => editRun(job,edit,data.historical_baseline_restore === true)).join('')}${edits.length > 1 ? `<details class="edit-older"><summary>较早的 ${edits.length - 1} 次剪辑</summary>${job.archived || !olderIds.length ? '' : `<div class="edit-older-bar"><button type="button" class="secondary compact" data-clean-edits="${esc(olderIds.join(','))}">清理旧剪辑（${olderIds.length} 次）</button><small>只保留最新一次，连同本地文件一起删除，不动达芬奇里的时间线。</small></div>`}${edits.slice(1).map(edit => editRun(job,edit,data.historical_baseline_restore === true)).join('')}</details>` : ''}</div></section>`;
  }
  function bindEdit(data,choice,seq,request) {
    const job = data.job, form = $('#edit-form',$('#edit-panel'));
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
      $$('[data-recover-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;button.textContent = '正在恢复…';
        try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.recoverEdit)}/recheck`,{method:'POST',body:{recover_result:true}});toast('剪辑结果已恢复；工程核验未执行。');await route(true);}
        catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '恢复结果';}
      });
      $$('[data-recheck-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;button.textContent = '正在核验…';
        try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.recheckEdit)}/recheck`,{method:'POST',body:{}});toast('现有工程已重新核验，请查看状态和问题。');await route(true);}
        catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '重新核验';}
      });
      $$('[data-delete-edit]').forEach(button => button.onclick = () => deleteEdits([button.dataset.deleteEdit]));
      $$('[data-clean-edits]').forEach(button => button.onclick = () => deleteEdits(button.dataset.cleanEdits.split(',').filter(Boolean)));
      $$('[data-approve-edit]').forEach(button => button.onclick = () => {
        const edit = (job.edits || []).find(item => item.id === button.dataset.approveEdit);if (!edit) return;
        const scope = Array.isArray(edit.validation?.manual_scope) && edit.validation.manual_scope.length ? edit.validation.manual_scope : manualScope;
        const note = skillDelivery(edit) ? '剪辑已按技能交付。这里仅登记你的人工审查，不自动读回工程核验或计算素材去重；请在达芬奇里逐项看过再勾选。' : '工作台会先读回并保存达芬奇里的这条时间线，工程核验仍通过才记为完成。下面几项工程核验判断不了，请在达芬奇里逐项看过再勾选。';
        const modal = openModal(`确认完成：${edit.timeline_name}`,`<p class="context-note">${esc(note)}</p><fieldset class="material-picker"><legend>我已人工核对</legend><div class="material-options">${scope.map(item => `<label><input type="checkbox" name="reviewed" value="${esc(item)}" required>${esc(item)}</label>`).join('')}</div></fieldset><label class="field"><span>备注（可选）</span><textarea name="note" rows="2" maxlength="2000" placeholder="例如：已导出 1080×1920，字幕和混音已完成"></textarea></label>`,skillDelivery(edit) ? '确认人工审查完成' : '读回核验并确认完成',async modal => {
          const reviewed = $$('input[name="reviewed"]:checked',modal.form).map(input => input.value);
          const result = await send(`approve-${edit.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(edit.id)}/approval`,{approved:true,reviewed,note:modal.form.note.value.trim()});
          closeModal(true);
          toast(result.approved ? '已确认完成。' : `读回后核验结果为「${editLabels[result.edit?.status] || result.edit?.status}」，未确认完成，请查看问题。`,!result.approved);
          await route(true);
        });
        const submit = $('[data-submit]',modal.dialog), hint = $('[data-hint]',modal.dialog);
        const sync = () => {const done = $$('input[name="reviewed"]',modal.form).every(input => input.checked);submit.disabled = !done;hint.textContent = done ? '' : '逐项勾选后才能确认完成。';};
        $$('input[name="reviewed"]',modal.form).forEach(input => input.onchange = sync);sync();
      });
      $$('[data-revoke-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {await send(`revoke-${button.dataset.revokeEdit}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.revokeEdit)}/approval`,{approved:false});toast('已撤销完成确认。');await route(true);}
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
    if ((job.edits || []).some(edit => edit.status === 'running')) {
      const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
      const poll = async () => {
        if (!isCurrent()) return;
        if (document.hidden) {setTimeout(poll,5000);return;}
        try {
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (!isCurrent()) return;
          if (!(fresh.job.edits || []).some(edit => edit.status === 'running') && !view.modal) {await route(true);return;}
          $('#edit-runs').innerHTML = [...fresh.job.edits].reverse().map(edit => editRun(fresh.job,edit,fresh.historical_baseline_restore === true)).join('');bindRuns();mountActivityLogs(job.id);
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
    const modelReady = () => !loadingDefaults && !defaultsError && defaults &&
      clients?.providers?.[providerFor(selectedModel())]?.available && !clients.busy &&
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
      status.textContent = loadingDefaults || !defaults ? '正在检查剪辑客户端…' : clientsError ? `剪辑客户端检查失败：${clientsError}` : !model ? '请选择本次剪辑使用的兼容模型。' : !state?.available ? `${client}暂不可用：${state?.error || state?.label || '请在设置中检查客户端连接。'}` : !compatible(model,usesDefault() ? defaults.reasoning_effort : localSettings().effort) ? `${client}已连接；${catalog?.models_error || '模型或思考强度尚未确认，请在设置中检查。'}` : clients.busy ? `${client}可用；当前已有达芬奇 AI 任务，请等它结束。` : `${client}可用于 AI 剪辑。`;
      $('[data-edit-usage]',form).textContent = `AI 剪辑默认沿用全局模型与思考强度，支持 Codex 和 Claude。剪辑在后台运行，通常要 20–60 分钟，${model ? provider === 'codex' ? '使用 Codex 客户端当前登录账户的额度' : '使用已连接 Claude 账户的订阅额度' : '使用所选客户端当前账户的额度'}。期间请不要在达芬奇里操作；结束后工作台会自己读回时间线，列出缺素材和没有画面的位置。也可以「复制剪辑指令」，粘到 Codex 或 Claude Code 对话里边看边剪。`;
      $('[data-edit-override]',form).hidden = loadingDefaults || Boolean(defaultsError) || !defaults || inherited;
      form.model.disabled = loadingDefaults || Boolean(defaultsError) || inherited;
      form.effort.disabled = form.model.disabled || !form.model.value;
      $('[data-edit-default-refresh]',form).disabled = loadingDefaults || starting;
      $('button[type="submit"]',form).disabled = starting || job.archived || (job.edits || []).some(edit => edit.status === 'running') || !ready;
    };
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
    $('[data-edit-default-refresh]',form).onclick = loadEditDefaults;
    view.refreshEditDefaults = loadEditDefaults;
    void loadEditDefaults();
    const materials = () => pickedMaterials(form);
    request.then(status => {
      if (!form.isConnected || seq !== view.sequence) return;
      const box = $('[data-materials]',form);if (!box) return;
      materialPicker(box,status,new Set((job.timelines || []).map(item => item.name)),choice.edit_materials,data.product_settings.material_timelines);
    });
    const options = () => {
      const values = Object.fromEntries(new FormData(form));
      Object.assign(choice,{edit_timeline:values.timeline,edit_mode:values.mode,edit_instruction:values.instruction,edit_materials:materials()});
      return {timeline:values.timeline,mode:values.mode,instruction:values.instruction.trim(),materials:materials()};
    };
    form.addEventListener('input',options);
    form.addEventListener('change',options);
    $('[data-edit-settings]',form).addEventListener('click',options);
    $('[data-copy-handoff]',form).onclick = async event => {
      const button = event.currentTarget;button.disabled = true;setError(form,'');
      try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/handoff`,{method:'POST',body:options()});await copyText(result.prompt,'剪辑指令已复制，交接文件也已保存。');}
      catch (error) {setError(form,fail(error));}
      finally {button.disabled = false;}
    };
    form.onsubmit = async event => {
      event.preventDefault();setError(form,'');
      const body = {...options(),...(usesDefault() ? {} : localSettings())};
      const problem = materialProblem(body.materials);
      if (problem) {setError(form,problem);return;}
      if (!modelReady()) {setError(form,defaultsError || (loadingDefaults ? '正在读取全局默认配置，请稍候。' : '请选择本次剪辑使用的兼容模型，或修改全局设置。'));return;}
      const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在核对达芬奇并启动…';
      starting = true;updateEditControls();
      try {await send(`edit-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edit`,body);toast('AI 剪辑已开始，完成后这里会显示结果。');}
      catch (error) {setError(form,fail(error));starting = false;submit.textContent = '开始 AI 剪辑';updateEditControls();return;}
      if (seq === view.sequence) await route(true);
    };
  }
  const defaultCreator = (data,choice) => {
    const creators = data.creators || [];
    return choice.creator && creators.includes(choice.creator) ? choice.creator : [...(data.job.timelines || [])].reverse().find(item => creators.includes(item.creator))?.creator || creators[0] || '';
  };
  function timelineSection(data,choice,selected) {
    const job = data.job, timelines = [...(job.timelines || [])].reverse(), creators = data.creators || [], config = data.product_settings;
    const creator = defaultCreator(data,choice);
    const form = !selected ? '<p class="context-note">先选用一版配音，再建时间线。</p>'
      : !creators.length ? `<p class="context-note">还没有达人名，请到 <a href="${esc(url('#production',job.product))}">制作单</a> 页的「达芬奇设置」里添加。</p>`
      : `<form id="timeline-form"><div class="form-row"><label class="field"><span>达人</span><select name="creator">${creators.map(name => `<option ${name === creator ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select></label><div></div></div><p class="context-note">将在项目「${esc(config.project)}」里新建 <code data-timeline-name>${esc(creator)}${esc(data.today)}-${esc(config.code)}-序号</code>（序号按项目里已有的时间线顺延）。配音放在单独的「配音」轨（音频 2），每个意群一个蓝色范围标记，备注里写着口播、中文和画面要求；视频轨留空，下一步交给剪辑。</p><div class="take-actions"><button type="submit" class="primary" ${job.archived ? 'disabled' : ''}>在达芬奇建时间线</button>${timelines.length && timelines[0].take_id !== job.selected_take ? '<span>当前选用的配音和最近一条时间线用的不同，会另建一条新时间线，旧的保留。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="timeline-panel"><h2>达芬奇时间线 <small>${timelines.length} 条</small></h2><p class="production-resolve" data-resolve-status>正在检查达芬奇…</p>${form}${timelines.map(item => `<article class="timeline-card" data-activity-source="${esc(item.unique_id)}"><div><strong>${esc(item.name)}</strong><small>${esc(item.creator)} · ${item.markers} 个意群标记 · ${(item.frames / Number(item.fps)).toFixed(1)} 秒 · 配音 ${esc(item.take_id)}${item.take_id === job.selected_take ? '（当前选用）' : ''} · ${esc(readableDate(item.built_at))}</small>${(item.warnings || []).length ? `<ul class="take-warnings">${item.warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}</div><button type="button" class="secondary" data-open-timeline="${esc(item.unique_id)}">在达芬奇打开</button></article>`).join('')}</section>`;
  }
  function bindTimeline(data,choice,seq,request) {
    const job = data.job, panel = $('#timeline-panel'), form = $('#timeline-form');
    resolveStatus($('[data-resolve-status]',panel),data.product_settings,request);
    if (form) {
      form.creator.onchange = () => {choice.creator = form.creator.value;$('[data-timeline-name]',form).textContent = `${form.creator.value}${data.today}-${data.product_settings.code}-序号`;};
      form.onsubmit = async event => {
        event.preventDefault();setError(form,'');choice.creator = form.creator.value;
        const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在达芬奇里建时间线…';
        try {const result = await send(`timeline-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/timeline`,{creator:form.creator.value});toast(`已在达芬奇建好时间线 ${result.timeline.name}。`);}
        catch (error) {setError(form,definitive(error) || error.status === 409 ? fail(error) : `结果尚未确认：${fail(error)}。请先到达芬奇里看是否已经建好，再决定是否重试。`);submit.disabled = false;submit.textContent = '在达芬奇建时间线';return;}
        if (seq === view.sequence) await route(true);
      };
    }
    $$('[data-open-timeline]',panel).forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/open`,{method:'POST',body:{unique_id:button.dataset.openTimeline}});toast(`已在达芬奇打开 ${result.name}。`);}
      catch (error) {toast(fail(error),true);}
      finally {button.disabled = false;}
    });
  }

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

  function renderJob(data,seq) {
    const job = data.job, takes = [...job.takes].reverse(), selected = job.takes.find(take => take.id === job.selected_take);
    const voices = (data.voices || []).filter(voice => !voice.disabled), models = data.models || [];
    const choice = view.choice[job.id] ||= {};
    const lastTake = job.takes[job.takes.length - 1];
    const voiceId = choice.voice_id || data.defaults?.[job.product] || lastTake?.voice_id || voices[0]?.voice_id || '__new';
    const modelId = choice.model_id || voices.find(voice => voice.voice_id === voiceId)?.model_id || data.default_model;
    const voiceName = id => {const voice = (data.voices || []).find(item => item.voice_id === id);return voice ? voiceLabel(voice) : id;};
    const modelName = id => models.find(item => item.id === id)?.name || id;
    // A take that has no timeline yet is selected and built in one go; rebuilding stays in the timeline step.
    const buildOnSelect = take => (data.creators || []).length > 0 && !job.archived && !(job.timelines || []).some(item => item.take_id === take.id);
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#production',job.product))}">${icon('back')}返回制作单</a><div class="page-heading"><div><span class="eyebrow">PRODUCTION JOB</span><h1>${esc(job.title)}</h1><p>${esc(job.product)} · ${esc(job.direction_id || '未指定方向')}${job.variant_label ? ` · ${esc(job.variant_label)}` : ''} · <span class="script-status ${['voiced','timeline','approved'].includes(job.status) ? 'published' : ''}">${esc(job.archived ? '已归档' : statusLabels[job.status] || job.status)}</span>${data.script ? ` · <a href="${esc(url(`#scripts/${encodeURIComponent(data.script.id)}`,job.product))}">脚本版本 ${esc(data.script.version)}</a>` : ' · 脚本版本已不可读'}</p></div><div class="scripts-start-actions"><button class="secondary" id="job-archive">${job.archived ? '恢复制作单' : '归档'}</button><button class="secondary danger" id="job-delete">永久删除</button></div></div>${recoverySection(job)}${problemsMarkup(job.problems)}<section class="section-card" id="job-script-panel"><div class="section-title-row"><h2>脚本 <small>${esc(job.variant_label || data.variant?.label || '')}${job.variant_label || data.variant?.label ? ' · ' : ''}${job.lines.length} 个意群</small></h2><button type="button" class="subtle" id="job-lines-edit">${icon('edit')}编辑画面要求</button></div><div class="script-variant markdown-body" id="job-script-body"></div><div class="script-lines"><h3>意群表 <small>含画面要求${selected ? '与配音时间' : ''}</small></h3>${linesTable(job.lines,selected ? selected.groups : null)}${selected ? `<p class="context-note">配音时间来自已选用的配音（${esc(sourceLabels[selected.alignment_source] || selected.alignment_source)}），单位秒；下一步建时间线时按这些时间打意群标记。</p>` : '<p class="context-note">选用一版配音后，这里显示每个意群在配音里的起止时间。</p>'}</div></section><section class="section-card" id="voice-panel"><h2>配音</h2>${job.voicing ? '<p class="production-busy">正在生成配音，完成后自动刷新…</p>' : ''}<form id="take-form" class="take-form"><div class="form-row"><label class="field"><span>音色</span><select name="voice">${voices.map(voice => `<option value="${esc(voice.voice_id)}" ${voice.voice_id === voiceId ? 'selected' : ''}>${esc(voiceLabel(voice))}${data.defaults?.[job.product] === voice.voice_id ? '（默认）' : ''}</option>`).join('')}<option value="__new" ${voiceId === '__new' ? 'selected' : ''}>新音色 ID…</option></select></label><label class="field"><span>模型</span><select name="model_id">${models.map(model => `<option value="${esc(model.id)}" ${model.id === modelId ? 'selected' : ''}>${esc(model.name)}</option>`).join('')}</select></label></div><div class="form-row" data-new-voice ${voiceId === '__new' ? '' : 'hidden'}><label class="field"><span>新音色 ID</span><input name="new_voice_id" pattern="[A-Za-z0-9]{10,64}" autocomplete="off" value="${esc(choice.new_voice_id || '')}"></label><label class="field"><span>备注（存入音色库）</span><input name="new_note" maxlength="500" value="${esc(choice.new_note || '')}"></label></div><div class="take-settings" data-settings></div><details class="take-text"><summary>送去配音的文字（${data.tts.characters} 字符${data.tts.substitutions ? `，已套用读法 ${data.tts.substitutions} 处` : ''}）</summary><pre>${esc(data.tts.text)}</pre></details><div class="take-actions"><button class="primary" type="submit" ${job.voicing || job.archived ? 'disabled' : ''}>生成配音</button><span>每次生成按字数消耗 ElevenLabs 额度（本条约 ${data.tts.characters} 字符），旧版本都会保留。</span></div><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="section-card" id="take-panel"><h2>配音版本 <small>${takes.length} 版</small></h2>${takes.length ? takes.map(take => `<article class="take-card ${take.id === job.selected_take ? 'selected' : ''}" data-activity-source="${esc(take.id)}"><div class="take-head"><div><strong>${esc(voiceName(take.voice_id))}</strong><small>${esc(modelName(take.model_id))} · ${Object.keys(settingLabels).filter(key => key in (take.settings || {})).map(key => `${settingLabels[key]} ${take.settings[key]}`).join(' · ')} · ${esc(seconds(take.duration))} · ${esc(readableDate(take.created_at))}</small></div>${take.id === job.selected_take ? '<span class="script-status published">已选用</span>' : `<button type="button" class="secondary" data-select-take="${esc(take.id)}" ${buildOnSelect(take) ? `data-build="${esc(defaultCreator(data,choice))}"` : ''} ${takeSelectable(take,job.lines.length) ? '' : 'disabled title="意群时间无效或对齐覆盖不足 60%，请先核对配音"'}>${buildOnSelect(take) ? `选用并建时间线（${esc(defaultCreator(data,choice))}）` : '选用这一版'}</button>`}</div><audio controls preload="none" src="${esc(audioURL(job.id,take.id,'mp3'))}"></audio>${(take.warnings || []).length ? `<ul class="take-warnings">${take.warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}<details><summary>意群时间（${esc(sourceLabels[take.alignment_source] || take.alignment_source)}）</summary><table class="production-table compact"><thead><tr><th>#</th><th>开始</th><th>结束</th><th>对上的字</th></tr></thead><tbody>${(take.groups || []).map(group => `<tr><td>${group.number}</td><td>${esc(seconds(group.start))}</td><td>${esc(seconds(group.end))}</td><td>${Math.round((group.coverage || 0) * 100)}%</td></tr>`).join('')}</tbody></table></details><div class="take-links">${take.wav ? `<a href="${esc(audioURL(job.id,take.id,'wav',true))}">下载 WAV</a>` : ''}<a href="${esc(audioURL(job.id,take.id,'mp3',true))}">下载 MP3</a><small>${esc(take.id)}</small></div></article>`).join('') : '<p class="context-note">还没有配音。</p>'}</section>${timelineSection(data,choice,selected)}${editSection(data,choice)}<details class="section-card production-history"><summary>制作记录（${job.history.length} 条）</summary><ol>${[...job.history].reverse().map(entry => `<li>${esc(readableDate(entry.at))} · ${esc(entry.note || entry.event)}</li>`).join('')}</ol></details>`;
    bindRecovery(job);
    renderScript($('#job-script-body'),data.variant?.markdown || fallbackScript(job.lines));
    const edits = job.edits || [], built = (job.timelines || []).some(item => selected && item.take_id === selected.id);
    mountSteps(job.id,[
      {key:'lines',label:'脚本',note:job.variant_label || `${job.lines.length} 个意群`,done:job.lines.length > 0,panels:[$('#job-lines-edit')?.closest('section')].filter(Boolean)},
      {key:'voice',label:'配音',note:job.voicing ? '生成中' : selected ? '已选用' : `${job.takes.length} 版`,done:!!selected,panels:[$('#voice-panel'),$('#take-panel')].filter(Boolean)},
      {key:'timeline',label:'达芬奇时间线',note:`${(job.timelines || []).length} 条`,done:built,panels:[$('#timeline-panel')].filter(Boolean)},
      {key:'edit',label:'AI 剪辑与人工审查',note:edits.some(edit => edit.status === 'running') ? '剪辑中' : job.status === 'approved' ? '已完成' : `${edits.length} 次`,done:job.status === 'approved',panels:[$('#edit-panel')].filter(Boolean)}
    ],job.voicing || (!selected && !edits.length) ? 'voice' : selected && !built ? 'timeline' : 'edit');
    const form = $('#take-form');
    const syncSettings = () => {
      const model = models.find(item => item.id === form.model_id.value), voice = voices.find(item => item.voice_id === form.voice.value);
      const stored = choice.settings?.[model.id] || (voice?.model_id === model.id ? voice.settings : null);
      $('[data-settings]',form).innerHTML = settingsMarkup(model,stored);
      $$('input[type="range"]',form).forEach(input => input.oninput = () => {$('output',input.closest('label')).textContent = Number(input.value).toFixed(2);});
      $('[data-new-voice]',form).hidden = form.voice.value !== '__new';
    };
    const remember = () => {
      const model = form.model_id.value, settings = Object.fromEntries($$('input[type="range"]',form).map(input => [input.name.replace('setting-',''),Number(input.value)]));
      Object.assign(choice,{voice_id:form.voice.value,model_id:model,new_voice_id:form.new_voice_id.value.trim(),new_note:form.new_note.value.trim()});
      (choice.settings ||= {})[model] = settings;return settings;
    };
    form.voice.onchange = () => {const voice = voices.find(item => item.voice_id === form.voice.value);if (voice?.model_id) form.model_id.value = voice.model_id;syncSettings();};
    form.model_id.onchange = syncSettings;syncSettings();
    form.onsubmit = async event => {
      event.preventDefault();setError(form,'');
      const settings = remember(), submit = $('button[type="submit"]',form);
      let voice = form.voice.value;
      submit.disabled = true;submit.textContent = '正在生成…（通常 10–60 秒）';
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
        setError(form,definitive(error) ? fail(error) : `结果尚未确认：${fail(error)}。重试会沿用同一请求，已取得的配音可接续恢复；结果仍未知时请先核实 ElevenLabs 记录。`);
        submit.disabled = false;submit.textContent = '生成配音';return;
      }
      if (seq === view.sequence) await route(true);
    };
    $$('[data-select-take]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      const creator = button.dataset.build;
      try {await send(`select-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/select`,{revision:data.revision,take_id:button.dataset.selectTake});}
      catch (error) {toast(fail(error),true);button.disabled = false;if (error.status === 409) await route(true);return;}
      if (!creator) {toast('已选用这一版配音。');await route(true);return;}
      button.textContent = '已选用，正在达芬奇里建时间线…';
      try {const result = await send(`timeline-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/timeline`,{creator});choice.creator = creator;choice.step = 'edit';toast(`已选用这一版配音，并在达芬奇建好时间线 ${result.timeline.name}。`);}
      catch (error) {choice.step = 'timeline';toast(`已选用这一版配音，但时间线${definitive(error) || error.status === 409 ? `没建成：${fail(error)}` : `结果尚未确认：${fail(error)}。请先到达芬奇里看是否已经建好`}。可在「达芬奇时间线」步骤重试。`,true);}
      if (seq === view.sequence) await route(true);
    });
    $('#job-delete')?.addEventListener('click',() => void deleteOrder('job',job));
    $('#job-archive').onclick = async () => {
      try {await send(`archive-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/archive`,{revision:data.revision,archived:!job.archived});toast(job.archived ? '制作单已恢复。' : '制作单已归档，配音文件保留。');await route(true);}
      catch (error) {toast(fail(error),true);}
    };
    $('#job-lines-edit').onclick = () => openModal('编辑画面要求',`<p class="context-note">口播与中文属于已采纳的脚本版本，这里只改每个意群的画面要求，供剪辑选材使用。</p>${job.lines.map(line => `<fieldset class="line-edit"><legend>#${line.number} ${esc(line.spoken)}</legend><label class="field"><span>画面要证明什么</span><textarea name="proof-${line.number}" rows="2" maxlength="2000">${esc(line.proof)}</textarea></label><div class="form-row"><label class="field"><span>素材标签</span><input name="tags-${line.number}" maxlength="2000" value="${esc(line.tags)}"></label><label class="field"><span>证据</span><input name="evidence-${line.number}" maxlength="2000" value="${esc(line.evidence)}"></label></div></fieldset>`).join('')}`,'保存画面要求',async modal => {
      const values = new FormData(modal.form);
      await once(modal,'lines',`/api/production/jobs/${encodeURIComponent(job.id)}/lines`,async () => ({revision:data.revision,lines:job.lines.map(line => ({number:line.number,proof:values.get(`proof-${line.number}`).trim(),tags:values.get(`tags-${line.number}`).trim(),evidence:values.get(`evidence-${line.number}`).trim()}))}));
      closeModal(true);toast('意群表已更新。');await route(true);
    });
    const request = readStatus('/api/production/resolve',10000).catch(error => ({ok:false,error:fail(error)}));
    bindTimeline(data,choice,seq,request);
    bindEdit(data,choice,seq,request);
    if (job.voicing) {
      // Re-render only once voicing has ended and no dialog is open: route() closes dialogs and would drop a half-filled form.
      const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
      const poll = async () => {
        if (!isCurrent()) return;
        if (document.hidden || view.modal) {setTimeout(poll,3000);return;}
        try {
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (!isCurrent()) return;
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
    const key = JSON.stringify([variantIndex,title,spoken,chinese]);
    if (modal.variant !== key) {modal.done = {};modal.requests = {};modal.variant = key;}
    const adopted = await once(modal,'script','/api/scripts',async () => ({revision:(await api(`/api/creation?product=${encodeURIComponent(product)}`)).revision,script:{title,text:spoken.join('\n'),translation_zh:chinese.length === spoken.length ? chinese.join('\n') : '',product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:taskRevision}}));
    let created;
    try {created = await once(modal,'job','/api/production/jobs',async () => ({revision:(await api('/api/production')).revision,job:{script_id:adopted.script.id,...(variantIndex === null ? {} : {variant_index:variantIndex})}}));}
    catch (error) {if (definitive(error)) {closeModal(true);toast(`脚本已采纳，但制作单没有创建：${fail(error)}。可在脚本版本里再点「制作视频」。`,true);location.hash = url(`#scripts/${encodeURIComponent(adopted.script.id)}`,product);return;}throw error;}
    closeModal(true);toast('已采纳为脚本版本，并创建制作单。');location.hash = jobLink(created.job);
  }
  // Checks the edited lines before anything is adopted; returns an error message or ''.
  function linesIssue(spoken,chinese) {
    if (!spoken.length) return '请至少填写一行要配音的口播。';
    if (chinese.length && chinese.length !== spoken.length) return `中文 ${chinese.length} 行，口播 ${spoken.length} 行：请逐行对应，或清空中文。`;
    return '';
  }
  function manualSplit(task,round,product,direction,reason) {
    const modal = openModal('手动指定要配音的口播',`<p class="context-note">${esc(reason)}</p><div class="production-format-choice"><p>可以让 AI 只整理排版：新开一轮，完成后逐字核对口播和中文，对不上就不采用。也可以直接把要配音的英文粘贴到下面，一行一个意群。</p><button type="button" class="secondary" data-format-round>一键整理格式</button></div><details class="production-candidate-source"><summary>查看本轮候选原文</summary><pre class="carousel-preview">${esc(round.output || '')}</pre></details><label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(`${direction ? direction + ' ' : ''}手动拆分`)}"></label><label class="field"><span>口播（一行一个意群）</span><textarea name="spoken" rows="8" spellcheck="false"></textarea></label><label class="field"><span>对应中文（可选，与口播逐行对应）</span><textarea name="chinese" rows="6"></textarea></label><p class="context-note">画面要求在制作单里补填。</p>`,'采纳并创建制作单',async modal => {
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
    const loose = variants.some(variant => variant.loose);
    const options = variants.map((variant,position) => `<label class="variant-option"><input type="radio" name="variant" value="${variant.index}" ${position === 0 ? 'checked' : ''}><span><strong>${esc(variant.label)}</strong><small>${variant.lines.length} 个意群${variant.problems.length ? ` · ${variant.problems.length} 处需核对` : ''}</small></span></label>`).join('');
    const content = `<label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(defaultTitle(variants[0]))}"></label><div data-variant-preview></div><details class="production-line-editor" data-line-editor><summary>调整意群（拆分、合并或修正断行）</summary><p class="context-note">一行一个意群。不改动时会带入意群表；改动后画面要求需在制作单里补填。</p><label class="field"><span>口播</span><textarea name="spoken" rows="8" spellcheck="false"></textarea></label><label class="field"><span>对应中文（可选，与口播逐行对应）</span><textarea name="chinese" rows="6"></textarea></label></details>`;
    const modal = openModal('选择要制作的变体',`<p class="context-note">选择变体即可查看对应脚本。口播每行是一个意群；采纳后正文固定为脚本版本。</p>${loose ? '<div class="production-format-choice"><p>这一轮没有按「口播（配音用）」代码块交付，下面是宽松识别的结果：意群按句拆分，请逐行核对。也可以让 AI 只整理排版（新开一轮，逐字核对后才采用）。</p><button type="button" class="secondary" data-format-round>一键整理格式</button></div>' : ''}${variantPickerBody(options,content)}`,'采纳并创建制作单',async modal => {
      const values = new FormData(modal.form), chosen = variants.find(item => String(item.index) === values.get('variant'));
      const spoken = edited ? splitLines(values.get('spoken')) : chosen.lines.map(line => line.spoken);
      const chinese = edited ? splitLines(values.get('chinese')) : chosen.lines.every(line => line.zh) ? chosen.lines.map(line => line.zh) : [];
      const issue = edited ? linesIssue(spoken,chinese) : '';if (issue) {setError(modal.dialog,issue);return;}
      await adoptAndProduce(modal,{task,round,taskRevision:data.task_revision,product,direction,title:values.get('title').trim(),spoken,chinese,variantIndex:chosen.index});
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
    return `<article class="edit-card ${esc(search.status)}" data-activity-source="${esc(search.id)}"><div class="take-head"><div><strong><span class="script-status ${search.status === 'completed' ? 'published' : ''}">${esc(editLabels[search.status] || search.status)}</span> 第 ${esc(search.slides.join('、'))} 张 · 每张 ${search.per_slide} 张候选</strong><small>${esc(client)} · ${effectiveModel ? `实际模型：${esc(stillModelLabel(effectiveModel))}` : `申请模型：${esc(stillModelLabel(search.requested_model || search.model))} · 实际型号尚未回报`}${effectiveModel && effectiveModel !== (search.requested_model || search.model) ? `（申请 ${esc(stillModelLabel(search.requested_model || search.model))}）` : ''}${effectiveEffort ? ` · 实际思考强度：${esc(effectiveEffort)}` : search.effort ? ` · 申请思考强度：${esc(search.effort)}` : ''} · ${provider === 'codex' ? '使用 Codex 客户端当前账户额度' : '使用 Claude 订阅额度'} · ${esc(search.materials.join('、'))} · 开始 ${esc(readableDate(search.started_at))}${search.finished_at ? ` · 结束 ${esc(readableDate(search.finished_at))}` : ''}</small></div>${search.status === 'running' ? `<button type="button" class="secondary" data-cancel-search="${esc(search.id)}">停止</button>` : item.archived ? '' : `<button type="button" class="subtle edit-delete" data-delete-search="${esc(search.id)}">${icon('trash')}删除</button>`}</div>${search.status === 'running' ? `<p class="production-busy">${esc(live.progress || '正在找底图…')}</p>${live.latest ? `<pre class="edit-live">${esc(live.latest)}</pre>` : ''}` : `<p class="edit-facts">新增候选 ${search.added || 0} 张</p>`}${(report.gaps || []).length ? `<h3>${esc(gapsLabel)}</h3><ul class="edit-gaps">${report.gaps.map(gap => `<li>第 ${esc(gap.slide)} 张：${esc(gap.need)}</li>`).join('')}</ul>` : ''}${(report.questions || []).length ? `<h3>待你确认</h3><ul class="edit-gaps">${report.questions.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}${warnings.length ? `<ul class="take-warnings">${warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}${search.error ? `<p class="ai-error">${esc(search.error)}</p>` : ''}${search.output ? `<details><summary>${esc(client)}的选图说明</summary><pre class="edit-output">${esc(search.output)}</pre></details>` : ''}${search.instruction ? `<p class="context-note">本次额外要求：${esc(search.instruction)}</p>` : ''}</article>`;
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
  // Batch archive / restore / permanent delete: the service judges every order on its own and reports each one.
  async function runBatch(action,targets) {
    if (!targets.length) return;
    const labels = {archive:'归档',restore:'恢复',delete:'永久删除'};
    const execute = async () => {
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
      view.selected = new Set(failed.map(entry => entry.id));
      await route(true);
      return result;
    };
    if (action !== 'delete') {await execute();return;}
    openModal('批量永久删除制作单',`<p class="context-note">将永久删除所选 ${targets.length} 张制作单（不需要先归档），以及它们在本地保存的配音、候选图、成图、成品包等全部文件。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程。</li><li>脚本版本和案例库保留，可以重新创建制作单。</li><li>仍有任务在运行的制作单会被跳过并说明原因。</li></ul><label class="field"><span><input type="checkbox" name="sure" required> 我已确认永久删除，不需要找回</span></label>`,'永久删除',async modal => {
      const result = await execute();
      if (result) closeModal(true);
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
    openModal('永久删除制作单',`<p class="context-note">将永久删除「${esc(preview.title || item.title || item.id)}」：${esc(parts.join('、'))}，以及它们在本地保存的全部文件（${preview.files} 个，共约 ${esc(size)}）。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程${kind === 'job' && preview.timelines ? `（已建的 ${preview.timelines} 条时间线需要的话请自行在达芬奇里处理）` : ''}。</li><li>脚本版本和案例库保留，可以重新创建制作单。</li>${preview.published ? '<li class="ai-error">这张图文单已登记发布；发布登记和成品包会一并删除，脚本里的发布状态不会自动改回。</li>' : ''}</ul><label class="field"><span><input type="checkbox" name="sure" required> 我已确认永久删除，不需要找回</span></label>`,'永久删除',async modal => {
      const result = await send(`order-delete-${item.id}`,`${base}/delete`,{});
      closeModal(true);
      const warnings = result.warnings || [];
      toast(warnings.length ? warnings[0] : '制作单及本地文件已永久删除。',warnings.length > 0);
      const back = $('.back-link');
      if (back) back.click();else location.hash = url('#production',item.product);
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
    const history = searches.length ? `<details class="edit-older still-history"><summary>以前的 AI 找画面记录（${searches.length} 次，已停用，可删除）</summary><div id="search-runs">${searchRuns(item)}</div></details>` : '<div id="search-runs" hidden></div>';
    return `<section class="section-card" id="still-panel"><h2>AI 按标签挑候选片段</h2><p class="context-note">AI 只读素材片段的标签和每一张的文字，为每一张挑几个候选片段：不看画面、不导出静帧、不占用达芬奇，通常 10–60 秒。挑好后在上面每一张点「从片段取帧」，推荐的片段排在最前，自己拖动取帧。</p><p class="production-resolve" data-resolve-status>正在检查达芬奇…</p><fieldset class="material-picker"><legend>素材时间线</legend><div data-materials>正在读取达芬奇里的时间线…</div></fieldset><div class="form-row"><label class="field"><span>每张挑几个片段</span><select name="per_slide">${[1,2,3,4,5].map(value => `<option ${value === (choice.per_slide || 3) ? 'selected' : ''}>${value}</option>`).join('')}</select></label></div><label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="2" maxlength="1000" placeholder="例如：第 1 张要能看到打开的箱子和七块磨板">${esc(choice.still_instruction || '')}</textarea></label><p class="context-note" data-clip-ai-model>正在读取全局默认模型…</p><div class="take-actions"><button type="button" class="primary" id="clip-ai-pick" ${item.archived ? 'disabled' : ''}>让 AI 按标签挑候选片段</button><span data-clip-ai-status role="status"></span></div><p class="ai-error" data-error role="alert" hidden></p>${summary}${history}</section>`;
  }
  // Large images open in place (no new tab); ctrl/shift/middle click still open the file itself.
  function openLightbox(source,label) {
    document.querySelector('dialog.image-lightbox')?.remove();
    const dialog = document.createElement('dialog');dialog.className = 'image-lightbox';
    dialog.innerHTML = `<button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button><img alt="${esc(label || '大图')}">`;
    $('img',dialog).src = source;
    document.body.append(dialog);
    const close = () => {try {dialog.close();} catch {}dialog.remove();};
    dialog.addEventListener('click',close);
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    dialog.showModal();
  }
  document.addEventListener('click',event => {
    const link = event.target.closest?.('a[target="_blank"]');
    const href = link?.getAttribute('href') || '';
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || !location.hash.includes('production')) return;
    if (!/^\/api\/production\/carousels\/CAR-[0-9a-f]{12}\/(images|outputs)\/[A-Z]{3}-[0-9a-f]{12}/.test(href)) return;
    event.preventDefault();openLightbox(href,link.querySelector('img')?.alt);
  });
  // Every slide is one tile of a grid, so five slides fit on one screen; only the first few candidates show, the rest fold.
  const PICK_VISIBLE = 4;
  function pickSection(item) {
    const card = (slide,candidate) => `<figure class="pick-card ${slide.base?.id === candidate.id ? 'chosen' : ''}"><a href="${esc(imageURL(item,candidate,'full'))}" target="_blank" rel="noopener" title="看原图"><img src="${esc(imageURL(item,candidate,'thumb'))}" alt="${esc(`第 ${slide.number} 张候选 ${candidate.name}`)}" loading="lazy"></a><figcaption><span>${candidate.origin === 'folder' ? `已有图片 · ${esc(candidate.name)}` : candidate.origin === 'clip' ? `片段取帧 · ${esc(candidate.marker || candidate.timeline || candidate.name)}` : candidate.origin === 'upload' ? `本地上传 · ${esc(candidate.original_name || candidate.name)}` : esc([candidate.timeline,candidate.marker].filter(Boolean).join(' · ') || candidate.name)}${candidate.evidence ? ' <em class="pick-evidence">效果证据</em>' : ''}</span>${candidate.reason ? `<small>${esc(candidate.reason)}</small>` : ''}</figcaption>${slide.base?.id === candidate.id ? `<button type="button" class="secondary compact" data-unpick="${slide.number}">取消选用</button>` : `<button type="button" class="primary compact" data-pick="${slide.number}" data-candidate="${esc(candidate.id)}">选为底图</button>`}</figure>`;
    const tile = slide => {
      const all = slide.candidates || [], chosenFirst = [...all.filter(candidate => slide.base?.id === candidate.id),...all.filter(candidate => slide.base?.id !== candidate.id)];
      const shown = chosenFirst.slice(0,PICK_VISIBLE), more = chosenFirst.slice(PICK_VISIBLE);
      return `<div class="pick-slide ${slide.base ? 'has-base' : ''}" data-pick-slide="${slide.number}" data-drop-hint="松开，导入到第 ${slide.number} 张"><div class="pick-head"><div><strong>${pad(slide.number)} ${esc(slide.title_lines.join(' / '))}</strong><small>${esc([slide.role,slide.source ? `素材来源：${slide.source}` : ''].filter(Boolean).join(' · '))}</small></div><span class="pick-state ${slide.base ? 'done' : ''}">${slide.base ? '已选底图' : all.length ? `${all.length} 张候选` : '待选'}</span></div><div class="pick-tools"><button type="button" class="subtle" data-clip-slide="${slide.number}">从片段取帧${view.clipCounts?.[String(slide.number)] ? `<em class="pick-ai">AI ${view.clipCounts[String(slide.number)]}</em>` : ''}</button><button type="button" class="subtle" data-import-slide="${slide.number}">导入底图</button></div>${shown.length ? `<div class="pick-grid">${shown.map(candidate => card(slide,candidate)).join('')}</div>` : '<p class="context-note">还没有候选图。</p>'}${more.length ? `<details class="pick-more"><summary>其余 ${more.length} 张候选</summary><div class="pick-grid">${more.map(candidate => card(slide,candidate)).join('')}</div></details>` : ''}</div>`;
    };
    return `<section class="section-card" id="pick-panel"><h2>逐张选底图</h2><div class="pick-slides">${item.slides.map(tile).join('')}</div></section>`;
  }
  const outputURL = (item,output,size = 'thumb') => `/api/production/carousels/${encodeURIComponent(item.id)}/outputs/${encodeURIComponent(output.id)}?size=${size}`;
  const currentOutput = slide => (slide.outputs || []).find(output => output.id === slide.output_id);
  const generating = item => (item.generations || []).some(generation => generation.status === 'running');
  const imageModes = {generated_visual:'生成视觉版',photo_preserving:'实拍保真版',layout_only:'仅文字排版'};
  const confirmedOutput = slide => {const output = currentOutput(slide);return Boolean(output?.approved && !output.stale);};
  const requirementsFor = (item,choice) => ({mode:'photo_preserving',editable_regions:['background','text'],protected_product:true,protected_evidence:true,hands_authorized:false,style:'',source:'当前逐张底图',instruction:'',postprocess:'layout_only',...(item.requirements || {}),...(choice.requirements || {}),aspect:choice.image_aspect || item.image_aspect || item.requirements?.aspect || '3:4'});
  const regionLabels = {background:'背景',text:'文字',hands:'手部',product:'产品外观'};
  const requirementsSummary = requirements => [imageModes[requirements.mode] || requirements.mode,
    (requirements.editable_regions || []).length ? `可改${requirements.editable_regions.map(key => regionLabels[key] || key).join('、')}` : '不改画面',
    [requirements.protected_product !== false ? '产品' : '',requirements.protected_evidence !== false ? '实拍证据' : ''].filter(Boolean).join('与').replace(/^(.+)$/,'保护$1'),
    requirements.style ? `风格：${requirements.style.length > 30 ? requirements.style.slice(0,30) + '…' : requirements.style}` : ''].filter(Boolean).join(' · ');
  function requirementsFields(requirements, disabled = false, prefix = 'image') {
    const off = disabled ? 'disabled' : '', regions = requirements.editable_regions || [];
    return `<fieldset class="carousel-requirements"><legend>制作要求</legend><div class="form-row"><label class="field"><span>制作模式</span><select name="${prefix}_mode" ${off}>${Object.entries(imageModes).map(([key,label]) => `<option value="${key}" ${requirements.mode === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label class="field"><span>整组风格</span><input name="${prefix}_style" maxlength="2000" value="${esc(requirements.style || '')}" placeholder="例如：粗犷暖光、清楚大字" ${off}></label></div><label class="field carousel-caption-style" data-caption-style="${prefix}" ${requirements.mode === 'layout_only' ? '' : 'hidden'}><span>文字样式</span><select name="${prefix}_caption_style" ${off}><option value="plain" ${requirements.caption_style !== 'rugged' ? 'selected' : ''}>普通文字</option><option value="rugged" ${requirements.caption_style === 'rugged' ? 'selected' : ''}>粗犷花字</option></select></label><label class="field carousel-caption-area"><span>仅文字排版：可用的留白区域</span><select name="${prefix}_caption_area" ${off}><option value="">请先确认图片留白</option><option value="top" ${requirements.caption_area === 'top' ? 'selected' : ''}>顶部留白</option><option value="bottom" ${requirements.caption_area === 'bottom' ? 'selected' : ''}>底部留白</option></select></label><div class="material-options carousel-requirement-options">${[['background','背景'],['text','文字与花字'],['hands','手部'],['product','产品外观']].map(([key,label]) => `<label><input type="checkbox" name="${prefix}_region" value="${key}" ${regions.includes(key) ? 'checked' : ''} ${off}>可改${label}</label>`).join('')}</div><div class="material-options carousel-requirement-options"><label><input type="checkbox" name="${prefix}_protected_product" ${requirements.protected_product !== false ? 'checked' : ''} ${off}>保护产品结构、动作与数量</label><label><input type="checkbox" name="${prefix}_protected_evidence" ${requirements.protected_evidence !== false ? 'checked' : ''} ${off}>保护实拍效果证据</label><label><input type="checkbox" name="${prefix}_hands_authorized" ${requirements.hands_authorized || requirements.hand_edit_allowed ? 'checked' : ''} ${off}>${requirements.authorized_hand_slides?.length ? '其他张手部也已获准改变' : '已授权改变手部'}</label></div>${requirements.authorized_hand_slides?.length || requirements.no_hand_slides?.length ? `<p class="context-note">逐张手部要求：${requirements.authorized_hand_slides?.length ? `第 ${esc(requirements.authorized_hand_slides.join('、'))} 张已授权改变手部` : ''}${requirements.no_hand_slides?.length ? `；第 ${esc(requirements.no_hand_slides.join('、'))} 张无手部画面` : ''}。</p>` : ''}<label class="field"><span>画面来源说明</span><input name="${prefix}_source" maxlength="2000" value="${esc(requirements.source || '')}" ${off}></label><p class="context-note">生成视觉版按商品参考核对外观、动作与数量；原实拍逐像素保真需要单独的实拍证据。普通文字或粗犷花字排版可复用已有画面，无需生图。首张只参考风格，每张仍使用自己的产品参考。</p></fieldset>`;
  }
  function requirementsFromForm(form,item,choice,prefix = 'image') {
    const value = name => form.elements.namedItem(`${prefix}_${name}`);
    const previous = requirementsFor(item,choice);
    const requirements = {...previous,mode:value('mode').value,style:value('style').value.trim(),source:value('source').value.trim(),editable_regions:$$(`input[name="${prefix}_region"]:checked`,form).map(input => input.value),protected_product:value('protected_product').checked ? previous.protected_product || true : false,protected_evidence:value('protected_evidence').checked ? previous.protected_evidence || true : false,hands_authorized:value('hands_authorized').checked,aspect:form.elements.namedItem('aspect')?.value || previous.aspect,instruction:form.elements.namedItem('instruction')?.value.trim() || '',postprocess:'layout_only'};
    if (value('caption_area')?.value) requirements.caption_area = value('caption_area').value;else delete requirements.caption_area;
    const captionStyle = value('caption_style');
    if (requirements.mode === 'layout_only' && (previous.caption_style || captionStyle?.dataset.explicit === 'true')) requirements.caption_style = captionStyle.value;else delete requirements.caption_style;
    return requirements;
  }
  function bindRequirementsMode(form,prefix = 'image') {
    const update = () => {
      const layoutOnly = form.elements.namedItem(`${prefix}_mode`).value === 'layout_only', field = $(`[data-caption-style="${prefix}"]`,form);
      if (field) field.hidden = !layoutOnly;
      const styleLabel = form.elements.namedItem(`${prefix}_style`)?.closest('label')?.querySelector('span');
      if (styleLabel) styleLabel.textContent = layoutOnly ? '整组场景风格（生成时适用）' : '整组风格';
    };
    form.elements.namedItem(`${prefix}_caption_style`)?.addEventListener('change',event => {event.target.dataset.explicit = 'true';});
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
  function imageRun(generation) {
    if (generation.status === 'completed') return '';
    const labels = {awaiting_first_approval:'首张待确认',...editLabels};
    return `<article class="image-run ${esc(generation.status)}" data-generation-id="${esc(generation.id)}"><div class="take-head"><strong><span class="script-status">${esc(labels[generation.status] || generation.status)}</span></strong>${generation.status === 'running' ? `<button type="button" class="secondary compact" data-cancel-generation="${esc(generation.id)}">停止出图</button>` : ''}</div>${generation.status === 'running' ? `<p class="production-busy" role="status">${esc(generation.live?.progress || '正在生成图片…')}</p>` : ''}${generation.error ? `<p class="ai-error">${esc(generation.error)}</p>` : ''}</article>`;
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
  function imageSlides(item) {
    const busy = generating(item), choice = view.choice[item.id] ||= {};
    return item.slides.map(slide => {
      const outputs = slide.outputs || [], disabled = item.archived || busy;
      const latest = [...outputs].reverse(), selectedOutput = currentOutput(slide);
      const front = selectedOutput?.approved && !selectedOutput.stale ? selectedOutput : latest.find(output => !output.stale) || latest[0];
      const others = latest.filter(output => output !== front), expanded = Boolean(choice.image_versions_open?.[slide.number]);
      const otherId = `image-versions-${item.id}-${slide.number}`;
      const card = output => {
        const selected = output.id === slide.output_id && output.approved, version = outputs.indexOf(output) + 1;
        return `<figure class="carousel-output ${selected ? 'current' : ''} ${output.stale ? 'stale' : ''}" data-output-id="${esc(output.id)}"${output.generation_id ? ` data-generation-result="${esc(output.generation_id)}"` : ''}><a href="${esc(outputURL(item,output,'full'))}" target="_blank" rel="noopener" title="查看成图原图" aria-label="查看第 ${slide.number} 张成图第 ${version} 版原图"><img src="${esc(outputURL(item,output))}" data-aspect="${output.aspect === '9:16' ? '9:16' : '3:4'}" alt="第 ${slide.number} 张成图第 ${version} 版"></a><div class="image-version-actions"><button type="button" class="${selected ? 'secondary' : 'primary'} compact" ${selected ? `data-revoke-output="${esc(output.id)}"` : output.approved ? `data-select-output="${esc(output.id)}" data-approve-output="${esc(output.id)}"` : `data-approve-output="${esc(output.id)}"`} data-output-slide="${slide.number}" ${disabled || output.stale ? 'disabled' : ''}>${output.stale ? '此图已失效' : selected ? '撤销确认' : '确认选用'}</button></div></figure>`;
      };
      return `<article class="image-slide" data-image-slide="${slide.number}"><div class="pick-head"><h3>${pad(slide.number)} ${esc(slide.title_lines.join(' / '))}</h3><button type="button" class="secondary compact" data-redo-slide="${slide.number}" ${disabled || !slide.base ? 'disabled' : ''}>${outputs.length ? '重做这一张' : '生成这一张'}</button></div>${front ? `<div class="image-version-stack ${expanded ? 'expanded' : ''}" data-image-stack="${slide.number}" data-carousel-id="${esc(item.id)}"><div class="image-stack-cover ${others.length ? 'stacked' : ''} ${others.length > 1 ? 'has-more' : ''}">${card(front)}</div>${others.length ? `<button type="button" class="subtle compact image-versions-toggle" data-image-versions-toggle="${slide.number}" data-image-count="${others.length}" aria-expanded="${expanded}" aria-controls="${esc(otherId)}">${expanded ? '收起' : `查看其他图片（${others.length}）`}</button><div class="image-version-others image-version-grid" id="${esc(otherId)}" ${expanded ? '' : 'hidden'}>${others.map(card).join('')}</div>` : ''}</div>` : '<p class="context-note">还没有成图。</p>'}</article>`;
    }).join('');
  }
  function imagesSection(data) {
    const item = data.carousel, choice = view.choice[item.id] ||= {}, runs = item.generations || [];
    const chosen = choice.image_slides || item.slides.filter(slide => slide.base && (!currentOutput(slide)?.approved || currentOutput(slide)?.stale)).map(slide => slide.number);
    const aspect = choice.image_aspect || item.image_aspect || runs.at(-1)?.aspect || '3:4';
    const firstReady = confirmedOutput(item.slides[0] || {});
    const continuation = runs.some(run => ['awaiting_first_approval','interrupted','cancelled','failed'].includes(run.status));
    return `<section class="section-card" id="carousel-images-panel"><div class="section-title-row"><h2>出图</h2><div class="carousel-image-heading-actions"><small>${item.slides.filter(confirmedOutput).length} / ${item.slides.length} 张已确认</small><button type="button" class="secondary compact" id="carousel-import-finished" ${item.archived || generating(item) ? 'disabled' : ''}>导入现有成品</button></div></div>
      <p class="production-resolve" id="carousel-images-service" role="status">正在检查出图服务…</p>
      <form id="carousel-image-form"><details class="carousel-requirements-toggle" id="carousel-requirements-toggle" ${choice.requirements_open ? 'open' : ''}><summary><strong>出图设置</strong><small>${esc(requirementsSummary(requirementsFor(item,choice)))}</small><span>修改</span></summary>${requirementsFields(requirementsFor(item,choice),item.archived || generating(item))}<div class="take-actions carousel-requirements-save"><button type="button" class="secondary compact" id="carousel-save-requirements" ${item.archived || generating(item) ? 'disabled' : ''}>保存制作要求</button><span>保存后，这个产品新建的图文单也默认沿用。</span></div></details>
      <fieldset class="material-picker"><legend>要制作哪几张</legend><div class="material-options">${item.slides.map(slide => `<label><input type="checkbox" name="image_slide" value="${slide.number}" ${chosen.includes(slide.number) && slide.base ? 'checked' : ''} ${!slide.base || item.archived || generating(item) ? 'disabled' : ''}>${pad(slide.number)} ${esc(slide.title_lines.join(' / '))}${!slide.base ? '（未选底图）' : ''}</label>`).join('')}</div></fieldset>
      <div class="form-row image-aspect-row"><label class="field"><span>成图比例</span><select name="aspect" ${item.archived || generating(item) ? 'disabled' : ''}><option value="3:4" ${aspect === '3:4' ? 'selected' : ''}>3:4</option><option value="9:16" ${aspect === '9:16' ? 'selected' : ''}>9:16</option></select></label><div></div></div>
      <label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="2" maxlength="2000" placeholder="例如：背景更简洁，产品主体放大，保留完整夹具与磨板">${esc(choice.image_instruction ?? item.requirements?.instruction ?? '')}</textarea></label>
      <p class="context-note">选好首张后，余图沿用它的风格。图片生成后可直接确认选用；后台处理时可以离开本页。</p>
      <div class="take-actions"><button type="submit" class="primary" disabled>生成选中的图</button><button type="button" class="secondary" id="carousel-generate-first" disabled>先制作首张</button>${continuation ? `<button type="button" class="secondary" id="carousel-continue-images" disabled>${firstReady ? '继续未完成的余图' : '首张确认后继续余图'}</button>` : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>
      <div id="carousel-image-runs">${[...runs].reverse().map(imageRun).join('')}</div><div id="carousel-image-slides">${imageSlides(item)}</div></section>`;
  }
  function bindImages(data) {
    const item = data.carousel, seq = view.sequence, choice = view.choice[item.id] ||= {}, form = $('#carousel-image-form');
    let service = null, preflight = null;
    $$('[data-image-versions-toggle]').forEach(button => button.onclick = () => setImageVersionsExpanded(button.closest('.image-version-stack'),button.getAttribute('aria-expanded') !== 'true'));
    const remember = () => {
      choice.image_slides = $$('input[name="image_slide"]:checked',form).map(input => Number(input.value));
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
      const unavailable = !service?.available || service.busy || generating(item) || item.archived;
      const layoutOnly = form.elements.namedItem('image_mode').value === 'layout_only' && service && service.source !== 'cockpit_image_edit';
      const blocked = item.archived || generating(item) || (!layoutOnly && unavailable);
      const submit = $('button[type="submit"]',form);if (submit) {submit.disabled = blocked || !$$('input[name="image_slide"]:checked',form).length;submit.textContent = layoutOnly ? '排版选中的图' : '生成选中的图';}
      $('#carousel-generate-first').disabled = blocked || !item.slides[0]?.base;
      const continueButton = $('#carousel-continue-images');if (continueButton) continueButton.disabled = blocked || !preflight?.first_ready;
      $$('[data-redo-slide]').forEach(button => {button.disabled = blocked || !item.slides.find(slide => slide.number === Number(button.dataset.redoSlide))?.base;});
    };
    updateSubmit();
    form.addEventListener('change',updateSubmit);
    readStatus('/api/production/codex-images',10000,true).then(status => {
      if (seq !== view.sequence || !form.isConnected) return;
      service = status;const node = $('#carousel-images-service');node.classList.toggle('problem',!status.available || status.busy);
      node.textContent = status.available ? status.busy ? '出图服务正在处理任务，结束后可以再生成。' : `${status.label || '图生图服务'}已就绪。` : status.error || '图生图服务暂不可用。';
      updateSubmit();
    }).catch(error => {if (seq === view.sequence && form.isConnected) {$('#carousel-images-service').textContent = `暂时无法检查出图服务：${fail(error)}`;}});
    if (confirmedOutput(item.slides[0] || {})) {
      api(`/api/production/carousels/${encodeURIComponent(item.id)}/preflight`).then(result => {
        if (seq !== view.sequence || !form.isConnected) return;
        preflight = result;updateSubmit();
      }).catch(() => {});
    }
    form.onsubmit = async event => {
      event.preventDefault();remember();setError(form,'');
      if (!choice.image_slides.length) {setError(form,'请勾选至少一张已有底图的图。');return;}
      const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在启动后台出图…';
      try {await send(`generate-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/generate`,{revision:data.revision,slides:choice.image_slides,aspect:choice.image_aspect,instruction:choice.image_instruction.trim(),requirements:choice.requirements});delete choice.image_slides;toast('已开始生成图片，完成后可直接确认选用。');await route(true);}
      catch (error) {setError(form,fail(error));submit.textContent = '生成选中的图';updateSubmit();}
    };
    $('#carousel-save-requirements').onclick = async event => {
      remember();const button = event.currentTarget;button.disabled = true;setError(form,'');
      try {await send(`requirements-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/requirements`,{revision:data.revision,requirements:choice.requirements});toast('制作要求已保存。');await route(true);}
      catch (error) {setError(form,fail(error));button.disabled = false;}
    };
    $('#carousel-import-finished').onclick = () => {
      const draft = choice.import_finished || {directory:'',model:'',requirements:{...requirementsFor(item,choice),mode:'generated_visual',editable_regions:[],source:'外部完成的生成／排版结果',hands_authorized:false}};
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
    $('#carousel-generate-first').onclick = event => startSubset(event.currentTarget,[item.slides[0].number]);
    $('#carousel-continue-images')?.addEventListener('click',event => startSubset(event.currentTarget,item.slides.filter(slide => slide.base && (!currentOutput(slide)?.approved || currentOutput(slide)?.stale)).map(slide => slide.number)));
    const approve = async (slide,output,approved,note = '',acknowledge = false,actor = 'human') => {
      await carouselMutation(data,() => send(`approval-${item.id}-${slide}`,`/api/production/carousels/${encodeURIComponent(item.id)}/approval`,{revision:data.revision,slide,output_id:output.id,approved,note,acknowledge,actor}));
      delete choice.image_slides;
      if (approved) delete choice.image_versions_open?.[slide];
      // Once the whole group is confirmed the package is made right away; if that fails the button in 领取与发布 still works.
      let packed = false;
      if (approved) {
        try {
          const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`), group = fresh.carousel;
          if (!group.archived && !generating(group) && group.slides.length && group.slides.every(entry => currentOutput(entry)?.approved && !currentOutput(entry)?.stale)) {
            await send(`delivery-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/delivery`,{revision:fresh.revision});packed = true;
          }
        } catch {}
      }
      toast(!approved ? `第 ${slide} 张已撤销确认。` : packed ? `第 ${slide} 张已确认选用。整组都已确认，成品包已自动整理好，可在「领取与发布」下载。` : `第 ${slide} 张已确认选用。`);await route(true);
    };
    $$('[data-approve-output]').forEach(button => button.onclick = async () => {
      const slide = item.slides.find(slide => slide.number === Number(button.dataset.outputSlide)), output = (slide.outputs || []).find(output => output.id === button.dataset.approveOutput);
      if (!output || output.stale) return;
      button.disabled = true;
      try {
        if (button.dataset.selectOutput) {await carouselMutation(data,() => send(`select-output-${item.id}-${slide.number}`,`/api/production/carousels/${encodeURIComponent(item.id)}/select-output`,{revision:data.revision,slide:slide.number,output_id:output.id}));delete choice.image_versions_open?.[slide.number];toast(`第 ${slide.number} 张已选用。`);await route(true);}
        else await approve(slide.number,output,true);
      } catch (error) {toast(fail(error),true);if (button.isConnected) button.disabled = false;}
    });
    $$('[data-revoke-output]').forEach(button => button.onclick = async () => {
      const slide = item.slides.find(slide => slide.number === Number(button.dataset.outputSlide)), output = (slide.outputs || []).find(output => output.id === button.dataset.revokeOutput);
      $$('[data-approve-output],[data-revoke-output]').forEach(node => {node.disabled = true;});
      try {await approve(slide.number,output,false,output.note || '');}catch (error) {toast(fail(error),true);await route(true);}
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
    const bindCancels = () => $$('[data-cancel-generation]').forEach(button => button.onclick = async () => {
      button.disabled = true;button.textContent = '正在停止…';
      try {await api(`/api/production/carousels/${encodeURIComponent(item.id)}/generations/${encodeURIComponent(button.dataset.cancelGeneration)}/cancel`,{method:'POST',body:{}});toast('已请求停止后台出图。');}
      catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '停止出图';}
    });
    bindCancels();
    if (generating(item)) {
      const poll = async () => {
        if (seq !== view.sequence || pageHash().split('?')[0] !== `#production/${item.id}`) return;
        try {
          const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`);
          if (seq !== view.sequence) return;
          if (!generating(fresh.carousel) && !view.modal) {await route(true);return;}
          $('#carousel-image-runs').innerHTML = [...(fresh.carousel.generations || [])].reverse().map(imageRun).join('');bindCancels();

        } catch {}
        setTimeout(poll,3000);
      };
      setTimeout(poll,3000);
    }
  }
  // Pick a base frame straight from a labeled material clip: scrub in the page, crop/rotate/brighten, save as a candidate.
  async function openClipPicker(data,item,slide) {
    const choice = view.choice[item.id] ||= {}, root = `/api/production/carousels/${encodeURIComponent(item.id)}`;
    const target = item.slides.find(entry => entry.number === slide);
    let names = finishedOnly(choice.still_materials || data.material_timelines || []);
    if (!names.length) {
      const status = await readStatus('/api/production/resolve',10000).catch(() => ({ok:false}));
      names = status.ok ? finishedOnly(status.timelines || []) : [];
    }
    if (!names.length) {toast(`没有找到名称带「${finishedMark}」的素材时间线，请先在「找底图」里选好素材时间线。`,true);return;}
    let clips = [], suggested = [], current = null, frame = 0, rotate = 0, brightness = 1, aspect = '3:4', scale = 1, centre = {x:.5,y:.5};
    let evidence = false, image = null, token = 0, timer = 0, query = '', showAll = false, reasons = new Map(), fromAI = false;
    const ready = () => {
      const on = Boolean(current && image), save = $in('[data-clip-save]'), submit = $in('[data-submit]');
      if (submit) submit.disabled = !on;
      if (save) save.disabled = !on;
    };
    const frameURL = (key,number,size) => `${root}/clips/${encodeURIComponent(key)}/frame?frame=${number}&size=${size}`;
    const modal = openModal(`第 ${slide} 张：从片段取帧`,`<div class="clip-picker">
      <p class="context-note">${esc(target ? [target.role,target.title_lines.join(' / ')].filter(Boolean).join(' · ') : '')}。左边按标签找片段，右边拖动取帧；取到的帧会存成这一张的候选，原素材和达芬奇不动。</p>
      <p class="context-note" data-clip-note role="status">正在读取素材片段清单…</p>
      <div class="clip-layout"><aside class="clip-side"><div class="clip-search"><input type="search" data-clip-search placeholder="按标签或名称筛选" aria-label="筛选片段"><button type="button" class="subtle compact" data-clip-refresh>刷新</button></div><div class="clip-list" data-clip-list></div></aside>
      <section class="clip-stage" data-clip-stage hidden><div class="clip-view" data-clip-view><canvas data-clip-canvas></canvas><div class="clip-crop" data-clip-crop hidden></div></div>
      <p class="clip-time" data-clip-time role="status"></p>
      <div class="clip-scrub"><button type="button" class="subtle compact" data-step="-10" aria-label="后退 10 帧">−10</button><button type="button" class="subtle compact" data-step="-1" aria-label="后退 1 帧">−1</button><input type="range" data-clip-slider aria-label="片段内位置"><button type="button" class="subtle compact" data-step="1" aria-label="前进 1 帧">+1</button><button type="button" class="subtle compact" data-step="10" aria-label="前进 10 帧">+10</button></div>
      <div class="clip-strip" data-clip-strip></div>
      <div class="clip-tools"><label>裁切<select data-clip-aspect><option value="3:4">3:4 竖图</option><option value="9:16">9:16</option><option value="none">不裁切</option></select></label><label>裁切大小<input type="range" min="50" max="100" value="100" data-clip-scale></label><label>亮度<input type="range" min="60" max="160" value="100" data-clip-brightness></label><button type="button" class="subtle compact" data-clip-rotate>顺时针转 90°</button><label class="clip-evidence"><input type="checkbox" data-clip-evidence>这帧是效果证据</label></div>
      <p class="clip-facts" data-clip-facts></p><div class="take-actions">${target?.base ? '' : '<button type="button" class="secondary" data-clip-save>只存为候选</button>'}<button type="button" class="subtle" data-clip-reveal>在达芬奇打开该片段</button></div></section></div></div>`,target?.base ? '替换这一张的底图' : '存为候选并选为底图',async current_modal => {
      const saved = await save(current_modal);
      const fresh = await api(root);
      await send(`base-${item.id}-${slide}`,`${root}/base`,{revision:fresh.revision,slide,candidate:saved.candidate.id});
      closeModal(true);toast(`第 ${slide} 张已取到帧并选为底图。`);await route(true);
    });
    modal.dialog.classList.add('clip-dialog');
    const $in = selector => $(selector,modal.dialog), note = $in('[data-clip-note]'), canvas = $in('[data-clip-canvas]'), context = canvas.getContext('2d');
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
    const go = number => {
      if (!current) return;
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
      note.textContent = '正在读取片段…';image = null;ready();
      try {current = await api(`${root}/clips/${encodeURIComponent(key)}`);}
      catch (error) {note.textContent = fail(error);return;}
      note.textContent = '';
      const [left,right] = current.source;
      $in('[data-clip-stage]').hidden = false;
      const slider = $in('[data-clip-slider]');slider.min = String(left);slider.max = String(right - 1);
      const offsets = current.strip || [], sheet = `${root}/clips/${encodeURIComponent(current.key)}/strip`, strip = $in('[data-clip-strip]');
      strip.style.setProperty('--tile-ratio',`${current.width} / ${current.height}`);
      strip.innerHTML = offsets.map((offset,index) => `<button type="button" data-frame="${left + offset}" aria-label="跳到第 ${offset} 帧" style="background-image:url('${esc(sheet)}');background-size:${offsets.length * 100}% 100%;background-position:${offsets.length > 1 ? index / (offsets.length - 1) * 100 : 0}% 0"></button>`).join('');
      $$('[data-frame]',modal.dialog).forEach(button => button.onclick = () => go(Number(button.dataset.frame)));
      $in('[data-clip-facts]').textContent = `${current.label} · ${current.timeline} · ${current.width}×${current.height} · ${current.label_status_text}${current.used_by.length ? ` · 这段素材已被 ${current.used_by.join('、')} 用过` : ''}`;
      renderList();go((left + right - 1) / 2);
    };
    const body = () => {
      const r = rectangle();
      return {slide,key:current.key,frame,rotate,brightness:Number(brightness.toFixed(2)),crop:r ? {x:r.x,y:r.y,w:r.w,h:r.h} : null,evidence};
    };
    const save = async active => {
      if (!current || !image) throw new Error('请先选一个片段并等这一帧显示出来。');
      const payload = body();
      return once(active,`clip-${slide}-${current.key}-${frame}-${JSON.stringify(payload)}`,`${root}/clip-frame`,async () => payload);
    };
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
      try {await save(modal);closeModal(true);toast(`已存为第 ${slide} 张的候选，可以在候选里选用。`);await route(true);}
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
        const listing = await api(`${root}/clips?${params}`);
        clips = listing.clips;
        const aiPicks = listing.ai_picks?.[String(slide)] || [];
        fromAI = aiPicks.length > 0;
        reasons = new Map(aiPicks.map(pick => [pick.key,pick.reason]));
        suggested = fromAI ? aiPicks.map(pick => pick.key) : (listing.suggested[String(slide)] || []);
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
    if (aiButton) aiButton.onclick = async () => {
      setError(panel,'');
      const materials = pickedMaterials(panel), problem = materialProblem(materials);
      if (problem) {setError(panel,problem);return;}
      const perSlide = Number($('select[name="per_slide"]',panel).value), instruction = $('textarea[name="instruction"]',panel).value.trim();
      Object.assign(choice,{still_materials:materials,per_slide:perSlide,still_instruction:instruction});
      aiButton.disabled = true;aiStatus.textContent = 'AI 正在按标签挑片段…（第一次要先读片段清单，通常 10–60 秒）';
      try {
        const result = await api(`${root}/clip-picks`,{method:'POST',body:{materials,per_slide:perSlide,instruction,request_id:crypto.randomUUID()}});
        toast(`AI 已挑好 ${result.count} 个候选片段，点每一张的「从片段取帧」查看。`);
        await route(true);
      } catch (error) {
        setError(panel,fail(error));
        aiButton.disabled = false;aiStatus.textContent = '';
      }
    };
    const pick = async (slide,candidate) => {
      try {await send(`base-${item.id}-${slide}`,`/api/production/carousels/${encodeURIComponent(item.id)}/base`,{revision:data.revision,slide,candidate});toast(candidate ? `第 ${slide} 张已选好底图。` : `第 ${slide} 张已取消底图。`);}
      catch (error) {toast(fail(error),true);}
      await route(true);
    };
    $$('[data-pick]').forEach(button => button.onclick = () => {button.disabled = true;void pick(Number(button.dataset.pick),button.dataset.candidate);});
    $$('[data-unpick]').forEach(button => button.onclick = () => {button.disabled = true;void pick(Number(button.dataset.unpick),null);});
    // Drag pictures from the desktop or a folder straight onto a slide's tile: one picture replaces its base, several become candidates.
    const carriesFiles = event => [...(event.dataTransfer?.types || [])].includes('Files');
    if (!item.archived) $$('[data-pick-slide]').forEach(tile => {
      const slide = Number(tile.dataset.pickSlide);
      let depth = 0;
      tile.addEventListener('dragenter',event => {if (!carriesFiles(event)) return;event.preventDefault();depth += 1;tile.classList.add('drop-over');});
      tile.addEventListener('dragover',event => {if (!carriesFiles(event)) return;event.preventDefault();event.dataTransfer.dropEffect = 'copy';});
      tile.addEventListener('dragleave',() => {depth = Math.max(0,depth - 1);if (!depth) tile.classList.remove('drop-over');});
      tile.addEventListener('drop',async event => {
        if (!carriesFiles(event)) return;
        event.preventDefault();event.stopPropagation();depth = 0;tile.classList.remove('drop-over');
        try {hideVideoDrop();} catch {}
        if (tile.dataset.uploading) return;
        const files = [...event.dataTransfer.files];
        tile.dataset.uploading = '1';tile.classList.add('uploading');
        try {
          const {made,picked} = await uploadSlideImages(item,slide,files,{pick:true,onStatus:text => {tile.dataset.dropHint = text;}});
          toast(picked ? `第 ${slide} 张已导入图片并选为底图。` : `已为第 ${slide} 张导入 ${made.length} 张图片，可在候选里选用。`);
          await route(true);
        } catch (error) {
          toast(fail(error),true);
          delete tile.dataset.uploading;tile.classList.remove('uploading');
        }
      });
    });
    $$('[data-clip-slide]').forEach(button => button.onclick = () => {
      button.disabled = true;
      void openClipPicker(data,item,Number(button.dataset.clipSlide)).catch(error => toast(fail(error),true)).finally(() => {button.disabled = false;});
    });
    $$('[data-import-slide]').forEach(button => button.onclick = async () => {
      const slide = Number(button.dataset.importSlide);
      let listing;
      try {listing = await api(`/api/production/carousels/${encodeURIComponent(item.id)}/folder-images`);}
      catch (error) {toast(fail(error),true);return;}
      const groups = new Map();for (const image of listing.images) {if (!groups.has(image.folder)) groups.set(image.folder,[]);groups.get(image.folder).push(image);}
      const modal = openModal(`第 ${slide} 张：导入底图`,`<section class="upload-box"><div class="upload-drop" data-upload-drop tabindex="0" role="button" aria-label="上传本地图片"><strong>上传本地图片</strong><span>点击选择、拖入图片，或按 Ctrl+V 粘贴截图 · PNG / JPG / WebP · 单张最大 25 MB，可一次选多张</span><input type="file" accept="image/png,image/jpeg,image/webp" multiple hidden data-upload-file></div><label class="upload-pick"><input type="checkbox" data-upload-pick ${item.slides.find(entry => entry.number === slide)?.base ? '' : 'checked'}> 只上传一张时直接选为这一张的底图（会清掉这一张的其他候选）</label><p class="context-note" data-upload-status role="status"></p></section><h3 class="folder-title">或者从图文素材文件夹添加</h3><p class="context-note">图片目录：${esc(listing.root)}${listing.start ? `（从 ${esc(listing.start)} 开始）` : ''}。点一张图，把它复制进这张制作单的候选；原文件不动。</p>${groups.size ? [...groups].map(([folder,images]) => `<h3 class="folder-title">${esc(folder || '（根目录）')}</h3><div class="pick-grid folder-grid">${images.map(image => `<button type="button" class="folder-pick" data-path="${esc(image.path)}" title="${esc(image.path)}"><img src="/api/production/folder-thumb?path=${encodeURIComponent(image.path)}" alt="${esc(image.name)}" loading="lazy"><span>${esc(image.name)}</span></button>`).join('')}</div>`).join('') : '<p class="context-note">这个目录里没有图片。</p>'}`,'关闭',async () => closeModal(true));
      const dropzone = $('[data-upload-drop]',modal.dialog), chooser = $('[data-upload-file]',modal.dialog), uploadStatus = $('[data-upload-status]',modal.dialog);
      const sendImages = async files => {
        if (modal.busy) return;
        modal.busy = true;dropzone.setAttribute('aria-disabled','true');
        try {
          const {made,picked} = await uploadSlideImages(item,slide,files,{pick:$('[data-upload-pick]',modal.dialog).checked,onStatus:text => {uploadStatus.textContent = text;}});
          modal.busy = false;closeModal(true);
          toast(picked ? `第 ${slide} 张已导入图片并选为底图。` : `已为第 ${slide} 张导入 ${made.length} 张图片，可在候选里选用。`);
          await route(true);
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
        try {await once(modal,`import-${tile.dataset.path}`,`/api/production/carousels/${encodeURIComponent(item.id)}/import`,async () => ({slide,path:tile.dataset.path}));modal.busy = false;closeModal(true);toast(`已添加到第 ${slide} 张的候选。`);await route(true);}
        catch (error) {modal.busy = false;setError(modal.dialog,fail(error));$$('.folder-pick',modal.dialog).forEach(node => {node.disabled = false;});}
      });
    });
  }
  const carouselDeliveries = data => data.carousel.deliveries || data.deliveries || [];
  const carouselPublication = data => data.publication || data.carousel.publication || null;
  const deliveryURL = (item,delivery) => `/api/production/carousels/${encodeURIComponent(item.id)}/deliveries/${encodeURIComponent(delivery.id)}/download`;
  function publicationPerformance(data,publication) {
    const sales = data.sales_data || publication.sales_data || {status:'not_found',metrics:{},evidence:{}};
    const item = {product:data.carousel.product,current:{metrics:{video_id:publication.post_id}},sales_data:sales};
    if (typeof renderPerformanceSection === 'function') return renderPerformanceSection(item);
    if (sales.status !== 'matched') return `<section class="section-card performance-empty"><h2>表现数据</h2><p>销售数据尚未匹配到此帖子，导入或同步销售数据后再查看。</p></section>`;
    return `<section class="section-card"><h2>表现数据</h2><div class="facts-grid">${[['views_display','播放量'],['product_clicks_display','商品点击'],['items_sold','售出件数'],['ctor_display','CTOR']].map(([key,label]) => `<div><span class="metric-label">${label}</span><span class="metric-value">${displayValue(sales.metrics?.[key])}</span></div>`).join('')}</div><p class="context-note">${esc(sales.evidence?.window || '统计窗口未提供')}</p></section>`;
  }
  function publicationHistory(item,deliveries) {
    const history = [...(item.publication_history || [])].reverse().sort((a,b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    if (!history.length) return '';
    return `<details class="publication-history" id="carousel-publication-history"><summary>发布登记历史（${history.length}）</summary><p class="context-note">这里保留每次保存的登记状态，也可能包含更正。请以当前登记为准。</p>${history.map(entry => {
      const index = deliveries.findIndex(delivery => delivery.id === entry.delivery_id);
      return `<article data-publication-history="${esc(entry.id || '')}"><small>${esc(readableDate(entry.updated_at))}</small><dl class="publication-facts"><dt>帖子 ID</dt><dd>${esc(entry.post_id)}</dd><dt>发布日期</dt><dd>${esc(entry.published_on)}</dd><dt>所用成品包</dt><dd>${index >= 0 ? `成品包 ${index + 1} · ` : ''}${esc(entry.delivery_id)}</dd>${entry.account ? `<dt>账号</dt><dd>${esc(entry.account)}</dd>` : ''}${entry.note ? `<dt>备注</dt><dd>${esc(entry.note)}</dd>` : ''}</dl></article>`;
    }).join('')}</details>`;
  }
  function deliverySection(data) {
    const item = data.carousel, deliveries = carouselDeliveries(data), publication = carouselPublication(data);
    const confirmed = item.slides.filter(confirmedOutput).length;
    const ready = item.slides.length > 0 && confirmed === item.slides.length;
    const canPack = ready && !item.archived && !generating(item);
    return `<section class="section-card" id="carousel-delivery-panel"><div class="section-title-row"><h2>成品领取与发布</h2><button type="button" class="primary" id="carousel-prepare-delivery" ${canPack ? '' : 'disabled'}>整理成品包</button></div><p class="context-note">${ready ? '成品包只包含按张号排列的已确认图片。新包整理后，旧包继续保留；发布时请选择实际使用的那一包。' : `还有 ${item.slides.length - confirmed} 张未工作台确认，请先在「出图」步骤逐张确认选用。已整理的旧包仍可领取。`}</p><p class="ai-error" data-error role="alert" hidden></p><div id="carousel-deliveries">${deliveries.length ? [...deliveries].reverse().map((delivery,index) => `<article class="delivery-package ${delivery.current ? 'current' : ''}" data-delivery-id="${esc(delivery.id)}" data-activity-source="${esc(delivery.id)}"><div class="delivery-package-head"><div><strong>成品包 ${deliveries.length - index} <span class="script-status ${delivery.current ? 'published' : ''}">${delivery.current ? '当前确认版本' : '历史版本 · 保留'}</span>${publication?.delivery_id === delivery.id ? ' <span class="script-status published">用于当前发布登记</span>' : ''}</strong><small>${esc(readableDate(delivery.created_at))} · ${delivery.slides?.length || 0} 张 · ${esc(delivery.aspect)}<br>${esc(delivery.filename || '')}</small></div><a class="primary compact" href="${esc(deliveryURL(item,delivery))}" download data-download-delivery="${esc(delivery.id)}">下载成品包</a></div><div class="take-actions"><button type="button" class="secondary compact" data-copy-delivery="${esc(delivery.id)}">${icon('copy')}复制英文发布文案</button><button type="button" class="secondary compact" data-publish-delivery="${esc(delivery.id)}" ${item.archived ? 'disabled' : ''}>${publication ? '修改发布登记' : '登记这包已发布'}</button></div></article>`).join('') : '<p class="delivery-empty">还没有整理成品包。</p>'}</div><div id="carousel-publication">${publication ? `<div class="publication-record"><div class="section-title-row"><h3>已登记发布</h3><button type="button" class="secondary compact" id="carousel-publication-edit" ${item.archived ? 'disabled' : ''}>修改发布登记</button></div><dl class="publication-facts"><dt>帖子 ID</dt><dd>${esc(publication.post_id)}</dd><dt>发布日期</dt><dd>${esc(publication.published_on)}</dd><dt>实际使用</dt><dd>${esc(deliveries.findIndex(delivery => delivery.id === publication.delivery_id) >= 0 ? `成品包 ${deliveries.findIndex(delivery => delivery.id === publication.delivery_id) + 1}` : '已登记的成品包')}</dd>${publication.account ? `<dt>账号</dt><dd>${esc(publication.account)}</dd>` : ''}${publication.post_url ? `<dt>帖子链接</dt><dd><a href="${esc(publication.post_url)}" target="_blank" rel="noopener">查看已发布帖子</a></dd>` : ''}${publication.note ? `<dt>备注</dt><dd>${esc(publication.note)}</dd>` : ''}</dl><small class="publication-history-note">修改登记会保留原登记历史，表现数据按帖子 ID 匹配。</small></div><div id="carousel-publication-performance">${publicationPerformance(data,publication)}</div>${publicationHistory(item,deliveries)}` : '<p class="context-note">整理完整成品包后，可登记实际发布的帖子 ID 和发布日期。</p>'}</div></section>`;
  }
  function bindDelivery(data) {
    const item = data.carousel, choice = view.choice[item.id] ||= {}, panel = $('#carousel-delivery-panel');
    const showCollection = delivery => {
      const card = $$('[data-delivery-id]',panel).find(node => node.dataset.deliveryId === delivery.id);if (!card) return;
      let state = $('[data-delivery-state]',card);
      if (!state) {state = document.createElement('p');state.className = 'delivery-package-state';state.dataset.deliveryState = delivery.id;$('.delivery-package-head',card).insertAdjacentElement('afterend',state);}
      state.textContent = `本地已导出 · 按工作台确认版本整理${delivery.collected_at ? ` · 已领取（${readableDate(delivery.collected_at)}）` : ' · 尚未领取'}${delivery.download_count ? ` · 下载 ${delivery.download_count} 次` : ''}`;
    };
    carouselDeliveries(data).forEach(showCollection);
    $('#carousel-publication').insertAdjacentHTML('afterbegin',`<p class="delivery-publication-state"><span class="script-status ${carouselPublication(data) ? 'published' : ''}">${carouselPublication(data) ? '已登记发布' : '未发布'}</span>${carouselPublication(data) ? '发布登记绑定实际使用的成品包。' : '本地导出、确认与领取不会自动登记发布。'}</p>`);
    $$('[data-download-delivery]',panel).forEach(link => link.addEventListener('click',() => {
      setTimeout(async () => {try {const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`);if (panel.isConnected) {data.revision = fresh.revision;carouselDeliveries(fresh).forEach(showCollection);}}catch {}},1500);
    }));
    $('#carousel-prepare-delivery').onclick = async event => {
      const button = event.currentTarget;button.disabled = true;button.textContent = '正在整理…';setError(panel,'');
      try {await send(`delivery-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/delivery`,{revision:data.revision});toast('成品包已本地导出，可以领取；实际发布后再登记帖子信息。');await route(true);}
      catch (error) {setError(panel,fail(error));if (error.status === 409) {try {data.revision = (await api(`/api/production/carousels/${encodeURIComponent(item.id)}`)).revision;}catch {}}button.disabled = false;button.textContent = '整理成品包';}
    };
    $$('[data-copy-delivery]',panel).forEach(button => button.onclick = () => {
      const delivery = carouselDeliveries(data).find(delivery => delivery.id === button.dataset.copyDelivery);
      copyText(delivery?.caption_en || '','这包的英文发布文案已复制');
    });
    const openPublication = preferred => {
      const deliveries = carouselDeliveries(data);if (!deliveries.length) return;
      const publication = carouselPublication(data), pending = choice.publication_pending;
      const draft = pending?.values || choice.publication_draft || publication || {};
      const selected = pending?.values.delivery_id || preferred || draft.delivery_id || deliveries.find(delivery => delivery.current)?.id || deliveries.at(-1)?.id;
      const modal = openModal(publication ? '修改图文发布登记' : '登记图文已发布',`<p class="context-note">选择你实际发布时使用的成品包。登记后按帖子 ID 匹配销售数据；修改会保留原登记历史。</p><label class="field"><span>实际使用的成品包</span><select name="delivery_id" required>${[...deliveries].reverse().map((delivery,index) => `<option value="${esc(delivery.id)}" ${delivery.id === selected ? 'selected' : ''}>成品包 ${deliveries.length - index} · ${esc(delivery.aspect)} · ${esc(readableDate(delivery.created_at))}${delivery.current ? ' · 当前确认版本' : ' · 历史版本'}</option>`).join('')}</select></label><div class="form-row"><label class="field"><span>帖子 ID（必填）</span><input name="post_id" inputmode="numeric" required pattern="[0-9]{6,25}" maxlength="25" value="${esc(draft.post_id || '')}" placeholder="6–25 位数字"></label><label class="field"><span>发布日期（必填）</span><input type="date" name="published_on" required value="${esc(draft.published_on || '')}"></label></div><label class="field"><span>帖子链接（可选）</span><input type="url" name="post_url" maxlength="2000" value="${esc(draft.post_url || '')}" placeholder="https://www.tiktok.com/@账号/photo/帖子ID"></label><label class="field"><span>账号（可选）</span><input name="account" maxlength="200" value="${esc(draft.account || '')}"></label><label class="field"><span>备注（可选）</span><textarea name="note" rows="2" maxlength="2000">${esc(draft.note || '')}</textarea></label>`,'保存发布登记',async modal => {
        const values = modal.publication_pending_values || Object.fromEntries(new FormData(modal.form));Object.keys(values).forEach(key => {values[key] = String(values[key]).trim();});
        choice.publication_draft = {...values};
        try {
          await once(modal,'publication',`/api/production/carousels/${encodeURIComponent(item.id)}/publication`,async () => ({revision:data.revision,...values}));
        } catch (error) {
          const uncertain = !definitive(error);
          $$('input[name],select[name],textarea[name]',modal.form).forEach(field => {field.disabled = uncertain;});
          if (uncertain) {modal.publication_pending_values = {...values};choice.publication_pending = {request:modal.requests.publication,values:{...values}};}
          else {delete modal.publication_pending_values;delete choice.publication_pending;}
          $('[data-hint]',modal.dialog).textContent = uncertain ? '请直接重试原登记，结果明确前内容暂不可修改。' : '';
          if (error.status === 409) {try {data.revision = (await api(`/api/production/carousels/${encodeURIComponent(item.id)}`)).revision;}catch {}}
          throw Object.assign(new Error(`${fail(error)} ${uncertain ? '请直接重试原登记；结果明确前登记内容暂不可修改。' : '本次输入已保留，可修改后重试。'}`),{status:error.status});
        }
        delete choice.publication_draft;delete choice.publication_pending;closeModal(true);toast(publication ? '发布登记已更新，原记录保留。' : '已登记发布，表现数据将按帖子 ID 匹配。');await route(true);
      });
      if (pending) {
        modal.requests = {publication:pending.request};modal.publication_pending_values = {...pending.values};
        $$('input[name],select[name],textarea[name]',modal.form).forEach(field => {field.disabled = true;});
        setError(modal.dialog,'上次登记的结果尚未确认，请直接重试原登记。原输入已保留，结果明确前暂不可修改。');
        $('[data-hint]',modal.dialog).textContent = '请直接重试原登记。';
      }
      const remember = () => {choice.publication_draft = Object.fromEntries(new FormData(modal.form));};
      modal.form.addEventListener('input',remember);modal.form.addEventListener('change',remember);
    };
    $$('[data-publish-delivery]',panel).forEach(button => button.onclick = () => openPublication(button.dataset.publishDelivery));
    $('#carousel-publication-edit')?.addEventListener('click',() => openPublication());
  }
  function slidesTable(slides) {
    return `<div class="production-table-wrap"><table class="production-table carousel-table"><thead><tr><th>张</th><th>作用</th><th>叠字</th><th>正文</th><th>中文</th><th>素材来源</th><th>禁写</th><th>底图</th></tr></thead><tbody>${slides.map(slide => `<tr><td>${slide.number}</td><td>${esc(slide.role || '—')}</td><td class="spoken">${slide.title_lines.map(esc).join('<br>')}</td><td>${esc(slide.body || '—')}</td><td>${esc([slide.title_zh,slide.body_zh].filter(Boolean).join('：') || '—')}</td><td>${esc(slide.source || '待补')}</td><td>${esc(slide.forbidden || '—')}</td><td>${slide.base ? esc(slide.base.name || '已选') : '待选'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function renderCarousel(data) {
    const item = data.carousel;
    $('#breadcrumb').innerHTML = '内容创作 <span>/</span> 图文制作单';
    view.clipCounts = data.clip_picks?.counts || null;
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#production',item.product))}">${icon('back')}返回制作单</a><div class="page-heading"><div><span class="eyebrow">PHOTO CAROUSEL</span><h1>${esc(item.title)}</h1><p>${esc(item.product)} · ${esc(item.direction_id || '未指定方向')}${item.variant_label ? ` · ${esc(item.variant_label)}` : ''} · <span class="script-status">${esc(carouselLabel(item))}</span>${data.script ? ` · <a href="${esc(url(`#scripts/${encodeURIComponent(data.script.id)}`,item.product))}">脚本版本 ${esc(data.script.version)}</a>` : ' · 脚本版本已不可读'}</p></div><div class="scripts-start-actions"><button class="secondary" id="carousel-archive">${item.archived ? '恢复制作单' : '归档'}</button><button class="secondary danger" id="carousel-delete">永久删除</button></div></div>${problemsMarkup(item.problems)}<section class="section-card"><div class="section-title-row"><h2>逐张表 <small>${item.slides.length} 张</small></h2><button type="button" class="subtle" id="carousel-slides-edit">${icon('edit')}编辑作用、素材来源与禁写</button></div>${slidesTable(item.slides)}<p class="context-note">叠字、正文和中文属于已采纳的脚本版本，不在这里改；改稿请另存脚本新版本再建制作单。已有成品可在「出图」步骤用「导入现有成品」登记，再逐张确认选用；从素材开始制作时，先选底图，再生成首张并继续余图。</p></section>${stillSection(data)}${pickSection(item)}<section class="section-card"><div class="section-title-row"><h2>图文发布文案</h2><button type="button" class="subtle" id="carousel-caption-copy">${icon('copy')}复制英文</button></div><p class="carousel-caption">${esc(item.caption_en || '—')}</p><p class="context-note">${esc(item.caption_zh || '')}</p></section><details class="section-card production-history"><summary>制作记录（${item.history.length} 条）</summary><ol>${[...item.history].reverse().map(entry => `<li>${esc(readableDate(entry.at))} · ${esc(entry.note || entry.event)}</li>`).join('')}</ol></details>`;
    $('#carousel-caption-copy').onclick = () => copyText(item.caption_en || '','图文发布文案已复制');
    $('#pick-panel').insertAdjacentHTML('afterend',imagesSection(data)+deliverySection(data));
    const count = item.slides.length, based = item.slides.filter(slide => slide.base).length, found = item.slides.filter(slide => slide.base || (slide.candidates || []).length).length;
    const confirmed = item.slides.filter(slide => currentOutput(slide)?.approved && !currentOutput(slide)?.stale).length, publication = carouselPublication(data), packs = carouselDeliveries(data).length;
    // Finding and picking bases share one step: candidates first once there are any, the search form folds away when every slide has some.
    const searching = (item.searches || []).some(search => search.status === 'running'), stillForm = $('#still-form');
    // Picking from the clips is the main path; the slow AI search is a folded extra below it.
    $('#still-panel')?.before($('#pick-panel'));
    if (stillForm && !searching) {
      const again = document.createElement('details');again.className = 'still-search-again';
      again.innerHTML = `<summary>${found ? '再让 AI 自动找一次候选' : '让 AI 自动找候选'}（较慢，通常 10–30 分钟；也可以在上面每一张点「从片段取帧」自己挑）</summary>`;
      stillForm.before(again);again.append(stillForm);
    }
    $('#carousel-requirements-toggle')?.addEventListener('toggle',event => {(view.choice[item.id] ||= {}).requirements_open = event.currentTarget.open;});
    mountSteps(item.id,[
      {key:'slides',label:'逐张表',note:`${count} 张`,panels:[$('#carousel-slides-edit')?.closest('section')].filter(Boolean)},
      {key:'pick',label:'找图与选底图',note:searching ? '正在找候选' : `${based}/${count} 已选`,done:count > 0 && based === count,panels:[$('#pick-panel'),$('#still-panel')].filter(Boolean)},
      {key:'images',label:'出图',note:generating(item) ? '出图中' : `${confirmed}/${count} 已确认`,done:count > 0 && confirmed === count,panels:[$('#carousel-images-panel')].filter(Boolean)},
      {key:'delivery',label:'领取与发布',note:publication ? '已登记发布' : packs ? `${packs} 个成品包` : '',done:!!publication,panels:[$('#carousel-delivery-panel'),$('#carousel-caption-copy')?.closest('section')].filter(Boolean)}
    ],confirmed === count && count ? 'delivery' : based < count && !item.slides.some(slide => (slide.outputs || []).length) ? 'pick' : 'images');
    bindStills(data);
    bindImages(data);
    bindDelivery(data);
    $('#carousel-delete')?.addEventListener('click',() => void deleteOrder('carousel',item));
    $('#carousel-archive').onclick = async () => {
      try {await send(`carousel-archive-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/archive`,{revision:data.revision,archived:!item.archived});toast(item.archived ? '图文制作单已恢复。' : '图文制作单已归档。');await route(true);}
      catch (error) {toast(fail(error),true);}
    };
    $('#carousel-slides-edit').onclick = () => openModal('编辑作用、素材来源与禁写',`<p class="context-note">这三项决定下一步怎么找底图、怎么出图；叠字和正文不在这里改。</p>${item.slides.map(slide => `<fieldset class="line-edit"><legend>${String(slide.number).padStart(2,'0')} ${esc(slide.title_lines.join(' / '))}</legend><div class="form-row"><label class="field"><span>作用</span><input name="role-${slide.number}" maxlength="2000" value="${esc(slide.role)}"></label><label class="field"><span>禁写</span><input name="forbidden-${slide.number}" maxlength="2000" value="${esc(slide.forbidden)}"></label></div><label class="field"><span>素材来源</span><textarea name="source-${slide.number}" rows="2" maxlength="2000">${esc(slide.source)}</textarea></label></fieldset>`).join('')}`,'保存',async modal => {
      const values = new FormData(modal.form);
      await once(modal,'slides',`/api/production/carousels/${encodeURIComponent(item.id)}/slides`,async () => ({revision:data.revision,slides:item.slides.map(slide => ({number:slide.number,role:values.get(`role-${slide.number}`).trim(),source:values.get(`source-${slide.number}`).trim(),forbidden:values.get(`forbidden-${slide.number}`).trim()}))}));
      closeModal(true);toast('逐张表已更新。');await route(true);
    });
  }
  async function fromCandidateCarousel(task,round) {
    const product = task.product || task.context?.product || currentProduct(), direction = task.context?.direction_id || '';
    if (!product) {toast('请先选择一个具体产品。',true);return;}
    let data;
    try {data = await api(`/api/production/carousel-variants?task_id=${encodeURIComponent(task.task_id)}&round_id=${encodeURIComponent(round.round_id)}`);}
    catch (error) {toast(fail(error),true);return;}
    const items = data.carousels, defaultTitle = item => `${direction ? direction + ' ' : ''}${item.label} · 图文`;
    const options = items.map((item,position) => `<label class="variant-option"><input type="radio" name="variant" value="${item.index}" ${position === 0 ? 'checked' : ''}><span><strong>${esc(item.label)}</strong><small>${item.slides.length} 张${item.problems.length ? ` · ${item.problems.length} 处需核对` : ''}</small></span></label>`).join('');
    const content = `<label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(defaultTitle(items[0]))}"></label><div data-variant-preview></div>`;
    const modal = openModal('选择要制作的图文版',`<p class="context-note">选择图文版即可查看叠字、正文和发布文案。采纳后保存为脚本版本，再创建图文制作单。</p>${variantPickerBody(options,content)}`,'采纳并创建图文制作单',async modal => {
      const chosen = items.find(item => String(item.index) === new FormData(modal.form).get('variant'));
      const title = new FormData(modal.form).get('title').trim();
      if (modal.variant !== chosen.index) {modal.done = {};modal.requests = {};modal.variant = chosen.index;}
      const adopted = await once(modal,'script','/api/scripts',async () => ({revision:(await api(`/api/creation?product=${encodeURIComponent(product)}`)).revision,script:{title,text:chosen.text,translation_zh:chosen.translation_zh,product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:data.task_revision}}));
      let created;
      try {created = await once(modal,'carousel','/api/production/carousels',async () => ({revision:(await api('/api/production')).revision,carousel:{script_id:adopted.script.id,variant_index:chosen.index}}));}
      catch (error) {if (definitive(error)) {closeModal(true);toast(`脚本已采纳，但图文制作单没有创建：${fail(error)}。可在脚本版本里再点「制作图文」。`,true);location.hash = url(`#scripts/${encodeURIComponent(adopted.script.id)}`,product);return;}throw error;}
      closeModal(true);toast('已采纳图文版，并创建图文制作单。');location.hash = carouselLink(created.carousel);
    });
    modal.dialog.classList.add('variant-picker-dialog');
    const preview = () => {
      const chosen = items.find(item => String(item.index) === new FormData(modal.form).get('variant'));
      $('[data-variant-preview]',modal.dialog).innerHTML = problemsMarkup(chosen.problems) + slidesTable(chosen.slides) + `<p class="context-note">发布文案：${esc(chosen.caption_en)}</p>`;
      revealVariantContent(modal,chosen.label);
    };
    let titleEdited = false;
    modal.form.title.oninput = () => {titleEdited = true;};
    $$('input[name="variant"]',modal.dialog).forEach(input => input.onchange = () => {
      if (!titleEdited) modal.form.title.value = defaultTitle(items.find(item => String(item.index) === input.value));
      preview();
    });
    preview();
  }
  async function fromScriptCarousel(script) {
    const scriptId = script.script_id || script.id;
    // Scripts adopted from an early 图文版 candidate still create their job directly from it.
    const earlyCarousel = /^01\s/.test(String(script.text || '').trim());
    const wait = ms => new Promise(resolve => setTimeout(resolve,ms));
    const preview = (item,table) => `${problemsMarkup([...(table.note ? [table.note] : []),...item.problems])}${slidesTable(item.slides)}<p class="context-note">发布文案：${esc(item.caption_en || '（未给出）')}</p>`;
    openModal('用图文技能生成逐张表',`<p class="context-note">图文版不再随写稿附带。工作台把这条口播稿和它的意群表交给图文技能（tiktok-photo-commerce），由它合并成逐张的叠字、正文和图文发布文案；你核对表格后再创建图文制作单。生成可能要几分钟。</p><pre class="carousel-preview">${esc(String(script.text || '').slice(0,1200))}</pre><label class="field"><span>补充要求（选填）</span><textarea name="instruction" rows="2" maxlength="2000" placeholder="例如：只做 4 张；首图突出整套价值。"></textarea></label><div data-table-progress></div>${earlyCarousel ? '<p class="context-note">这条脚本是早期从图文版采纳的，也可以<button type="button" class="subtle" data-early>直接按已采纳的图文版创建</button>。</p>' : ''}`,'生成逐张表',async modal => {
      const progress = $('[data-table-progress]',modal.dialog);
      const started = await once(modal,'table','/api/production/carousel-tables',async () => ({script_id:scriptId,instruction:String(new FormData(modal.form).get('instruction') || '').trim()}));
      let table = started.table;
      while (table.status === 'running') {
        progress.innerHTML = `<p class="context-note">图文技能处理中：${esc(table.progress || '请稍候')}</p>`;
        await wait(2500);
        if (view.modal !== modal) return;
        table = (await api(`/api/production/carousel-tables/${encodeURIComponent(table.id)}`)).table;
      }
      if (table.status !== 'completed' || !table.carousels?.length) throw Object.assign(new Error(table.error || '逐张表没有生成，请重试。'),{status:422});
      const item = table.carousels[0];
      closeModal(true);
      openModal('核对图文逐张表',`<p class="context-note">这是图文技能按意群表合并出的逐张表。作用、素材来源和禁写创建后还能在制作单里改；叠字和正文需要改就重新生成。</p>${preview(item,table)}`,'创建图文制作单',async review => {
        const created = await once(review,'carousel','/api/production/carousels',async () => ({revision:(await api('/api/production')).revision,carousel:{script_id:scriptId,table_id:table.id}}));
        closeModal(true);toast('图文制作单已创建。');location.hash = carouselLink(created.carousel);
      });
    });
    $('[data-early]',view.modal?.dialog)?.addEventListener('click',async () => {
      try {
        const created = await api('/api/production/carousels',{method:'POST',body:{revision:(await api('/api/production')).revision,carousel:{script_id:scriptId},request_id:crypto.randomUUID()}});
        closeModal(true);toast('图文制作单已创建。');location.hash = carouselLink(created.carousel);
      } catch (error) {toast(fail(error),true);}
    });
  }

  async function fromScript(script) {
    const scriptId = script.script_id || script.id;
    let existing = [];
    try {existing = ((await api(`/api/production?product=${encodeURIComponent(script.product)}`)).jobs || []).filter(job => job.script_id === scriptId);} catch {}
    const lines = String(script.text || '').split('\n').map(line => line.trim()).filter(Boolean);
    openModal('为这条脚本创建制作单',`<p class="context-note">口播按行配音，每行是一个意群。来源候选里有逐行一致的意群表时会一起带入；没有的话，画面要求在制作单里补填。</p>${existing.length ? `<p class="context-note">这条脚本已有 ${existing.length} 张制作单：${existing.map(job => `<a href="${esc(jobLink(job))}">${esc(job.title)}（${esc(statusLabels[job.status] || job.status)}）</a>`).join('、')}。再建一张用于换音色或另剪一版。</p>` : ''}<div class="production-table-wrap"><table class="production-table"><thead><tr><th>#</th><th>将要配音的口播</th></tr></thead><tbody>${lines.map((line,index) => `<tr><td>${index + 1}</td><td class="spoken">${esc(line)}</td></tr>`).join('')}</tbody></table></div>`,'创建制作单',async modal => {
      const created = await once(modal,'job','/api/production/jobs',async () => ({revision:(await api('/api/production')).revision,job:{script_id:scriptId}}));
      closeModal(true);toast('制作单已创建。');location.hash = jobLink(created.job);
    });
  }

  window.addEventListener('focus',() => {if (!document.hidden) void view.refreshEditDefaults?.();});
  document.addEventListener('visibilitychange',() => {if (!document.hidden) void view.refreshEditDefaults?.();});
  window.addEventListener('hashchange',() => {if (!pageHash().startsWith('#production/')) view.activityTarget = null;});
  return {route,fromCandidate,fromScript,fromCandidateCarousel,fromScriptCarousel,modalOpen,captureListState};
})();
