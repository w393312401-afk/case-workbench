'use strict';

window.CaseAI = (() => {
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const pagePath = () => pageHash().split('?')[0];
  const taskRouteId = () => {
    try {return pagePath().startsWith('#ai/') ? decodeURIComponent(pagePath().slice(4)) : '';}
    catch {return '';}
  };
  const pageURL = hash => window.WorkbenchShell?.url(hash) || hash;
  const taskProduct = task => task.product || task.context?.product || (state.cases || []).find(item => item.case_id === task.case_id)?.product || '';
  const isWriting = task => !task?.action || task.action === 'variants';
  const resultName = task => ({review:'审稿结论',retrospective:'复盘结论'}[task?.action] || '候选结果');
  const contextTitle = task => task.display_title || (task.action === 'retrospective' ? task.context?.review_video?.headline || task.context?.review_video?.title : '') || task.context?.writing_topic || task.case_title || task.context?.title || task.context?.direction_id || task.case_id || `${taskProduct(task) || '产品'} · ${isWriting(task) ? '空白写稿' : resultName(task)}`;
  const scopedTasks = (tasks = ai.taskList?.tasks || []) => !window.WorkbenchShell?.product() ? tasks : tasks.filter(task => window.WorkbenchShell.matchesProduct(taskProduct(task)));
  function alignTaskProduct(task) {
    const shell = window.WorkbenchShell, product = taskProduct(task);
    if (!shell?.product() || !product || shell.matchesProduct(product)) return false;
    const query = pageHash().includes('?') ? `?${pageHash().split('?').slice(1).join('?')}` : '';
    location.hash = shell.url(`#ai/${encodeURIComponent(task.task_id)}${query}`, product);
    return true;
  }
  const actions = {variants:'生成变体',review:'一起审稿',retrospective:'一起复盘'};
  const requestHints = {variants:'例如：保持 S1 主打，写 3 条推进结构各不相同的版本（不只换开头），每条配 3 组标题行。',review:'例如：检查购买理由和每一段画面证据，指出最该修改的地方。',retrospective:'例如：结合播放、点击与成交数据，复盘这条案例的有效点和局限。'};
  function planPayload(modal) {
    if (!planActive(modal)) return {};
    const direction = planDirection(modal);
    return {variants:[...modal.variantSel].sort((a,b) => a - b).map(index => ({index,progression:direction.variants[index].progression ?? null})),new_variant_count:modal.newVariants};
  }
  const statuses = {queued:'排队中',running:'正在处理',completed:'已完成',failed:'执行失败',cancelled:'已取消',interrupted:'执行中断'};
  const managementStates = {pending_selection:'待选稿',needs_revision:'继续修改',adopted:'已采纳',abandoned:'已放弃'};
  const managementState = task => task.management_state || 'pending_selection';
  const managementFilterLabels = {pending_selection:'待选稿 / 确认结果',needs_revision:'继续修改 / 讨论',adopted:'已采纳 / 确认结果',abandoned:'已放弃'};
  const managementLabels = task => isWriting(task) ? managementStates : {pending_selection:'待确认结果',needs_revision:'继续讨论',adopted:'已确认结果',abandoned:'已放弃'};
  const managementLabel = task => managementLabels(task)[managementState(task)] || '待查看';
  const taskStarted = task => task.task_started_label || (task.created_at ? `${readableDate(task.created_at)} 发起` : '');
  const listViews = {active:'活跃',history:'历史',all:'全部记录'};
  const active = task => !!task?.busy || !!task?.stopping || ['queued','running'].includes(task?.status);
  const ai = {modal:null,status:null,options:null,optionsLoading:null,page:'',task:null,loading:false,sequence:0,mutation:0,polling:false,pending:new Map(),drafts:new Map(),settings:new Map(),errors:new Map(),cancelWaiting:new Set(),lastList:'',stream:null,streamRetryAt:0,streamFrame:null,queuedTask:null,taskList:null,taskListAt:0,taskListRequest:null,taskListRefreshAfter:false,navCount:null,selected:new Set(),deleteSelection:[],deleting:false};
  const effortNames = {none:'不启用',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高',ultra:'极致'};
  const skillNames = {auto:'自动使用统一技能','product-video-script-review':'带货文案创作与审查',none:'不使用技能'};
  const legacySkillNames = {'tiktok-video-script-voiceover':'视频脚本与口播（历史选择）',both:'两项技能配合（历史选择）'};
  const message = error => error?.message || '操作暂未完成。';
  const definitive = error => Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
  const statusText = task => task?.stopping ? '正在停止' : statuses[task?.status] || task?.status || '读取中';
  const taskName = task => `${actions[task.action] || 'AI 协作'} · ${contextTitle(task)}`;
  const shortVersion = value => String(value || '未提供').length > 18 ? `${String(value).slice(0,14)}…` : String(value || '未提供');
  function setText(selector,text) {const el = $(selector);if (el && el.textContent !== String(text || '')) el.textContent = text || '';}
  function updateNavCount(tasks) {
    const count = scopedTasks(tasks).filter(active).length;
    if (ai.navCount === count) return;
    ai.navCount = count;
    const badge = $('#ai-active-count');
    if (!badge) return;
    badge.textContent = String(count);
    badge.hidden = count === 0;
    badge.setAttribute('aria-label',`${count} 项运行中任务`);badge.title = `${count} 个 AI 任务运行中，在内容列表查看进度`;
    document.querySelectorAll('[data-ai-running]').forEach(node => {node.textContent = count ? String(count) : '';node.hidden = count === 0;});
  }
  function taskModel(task) {
    const round = task?.rounds?.at(-1);
    return round?.model || round?.requested_model || task?.model || null;
  }
  function taskSummary(task) {
    const model = taskModel(task);
    return {task_id:task.task_id,revision:task.revision,archived:!!task.archived,management_state:managementState(task),status:task.status,busy:task.busy,stopping:task.stopping,updated_at:task.updated_at,created_at:task.created_at,display_title:task.display_title,task_started_label:task.task_started_label,case_id:task.case_id,case_title:task.case_title,product:taskProduct(task),context:task.context,action:task.action,...(model ? {model} : {})};
  }
  function rememberTaskState(task) {
    if (!task?.task_id || !Array.isArray(ai.taskList?.tasks)) return;
    const tasks = ai.taskList.tasks, index = tasks.findIndex(item => item.task_id === task.task_id);
    const summary = taskSummary(task);
    if (index < 0) tasks.unshift(summary);
    else tasks[index] = {...tasks[index],...summary};
    updateNavCount(tasks);
  }
  function loadTaskList(maxAge = 5000,force = false) {
    if (ai.taskListRequest) {
      if (force) ai.taskListRefreshAfter = true;
      return ai.taskListRequest;
    }
    if (!force && ai.taskList && Date.now()-ai.taskListAt < maxAge) return Promise.resolve(ai.taskList);
    ai.taskListRequest = api('/api/ai/tasks').then(data => {
      const tasks = Array.isArray(data?.tasks) ? [...data.tasks] : [];
      const current = ai.task && taskRouteId() === ai.task.task_id ? ai.task : null;
      if (current?.task_id) {
        const index = tasks.findIndex(task => task.task_id === current.task_id);
        if (index < 0 && active(current)) {
          tasks.unshift(taskSummary(current));
        } else if (index >= 0 && Date.parse(current.updated_at || '') > Date.parse(tasks[index].updated_at || '')) {
          tasks[index] = {...tasks[index],...taskSummary(current)};
        }
      }
      ai.taskList = {...data,tasks};
      ai.taskListAt = Date.now();
      updateNavCount(tasks);
      return ai.taskList;
    }).finally(() => {
      ai.taskListRequest = null;
      if (ai.taskListRefreshAfter) {ai.taskListRefreshAfter = false;void loadTaskList(0,true).catch(() => {});}
    });
    return ai.taskListRequest;
  }
  function selectionInside(el) {const selection = window.getSelection();return selection && !selection.isCollapsed && (el.contains(selection.anchorNode) || el.contains(selection.focusNode));}
  function availabilityText() {
    if (!ai.status) return '正在检查 AI 服务…';
    return ai.status.available ? `${ai.status.label || 'AI 服务'} · 可以开始协作` : `AI 暂不可用：${ai.status.error || ai.status.label || '请检查本地服务配置后重新检查。'}`;
  }
  async function checkStatus(force = false) {
    if (ai.statusRequest) return ai.statusRequest;
    const maxAge = ai.status?.available ? 15000 : 3000;
    if (!force && ai.status && Date.now() - (ai.statusAt || 0) < maxAge) return ai.status;
    const revision = ai.statusRevision || 0;
    ai.statusRequest = (async () => {
      let status;
      try {status = await api('/api/ai/status');}
      catch (error) {status = {available:false,error:message(error)};}
      if ((ai.statusRevision || 0) === revision) {ai.status = status;ai.statusAt = Date.now();}
      return ai.status;
    })();
    try {return await ai.statusRequest;} finally {ai.statusRequest = null;}
  }
  function refreshAvailability(status) {
    ai.statusRevision = (ai.statusRevision || 0) + 1;
    ai.status = status;
    ai.statusAt = Date.now();
    ai.options = null;
  }
  function refreshDefaults(preferences) {
    ai.defaultsRevision = (ai.defaultsRevision || 0) + 1;
    ai.savedDefaults = {model:preferences?.model || null,reasoning_effort:preferences?.reasoning_effort || null};
    if (ai.options) {
      ai.options.default_settings = ai.savedDefaults;
      ai.options.defaults_configured = true;
      if (ai.options.defaults_error) {ai.options.defaults_error = '';ai.options.options_error = '';}
    }
    refreshSettingsViews();
    if ((ai.modal || ai.task) && (!ai.options || ai.options.options_error || (ai.savedDefaults.model && !selectedModel(ai.savedDefaults)))) void loadOptions(true).then(refreshSettingsViews);
  }
  function refreshSettingsViews() {
    if (ai.modal) renderSettings('ai-create',ai.modal.settings);
    if (ai.task) renderSettings('ai-continue',settingsForTask(ai.task));
    updateSettingsControls();
  }
  // Opening a page rereads the defaults (another tab may have changed them) but keeps the service's model list,
  // which it holds for 45 s; only the check buttons and a returning tab ask it to look for models again.
  async function loadOptions(force = false, reread = false) {
    if (ai.optionsLoading) return ai.optionsLoading;
    if (ai.options && !force && !reread) return ai.options;
    const revision = ai.defaultsRevision || 0;
    ai.optionsLoading = (async () => {
      try {
        const result = await api(`/api/ai/options${force ? '?refresh=1' : ''}`);
        const current = (ai.defaultsRevision || 0) === revision, defaults = current ? result.default_settings : ai.savedDefaults;
        const defaultsError = current ? result.defaults_error || '' : '';
        ai.options = {models:Array.isArray(result.models) ? result.models : [],skills:Array.isArray(result.skills) ? result.skills : [],default_settings:{model:defaults?.model || null,reasoning_effort:defaults?.reasoning_effort || null},defaults_configured:current ? !!result.defaults_configured : true,defaults_error:defaultsError,options_error:defaultsError,models_error:result.models_error || ''};
      } catch (error) {ai.options = {models:[],skills:[],default_settings:ai.savedDefaults || {model:null,reasoning_effort:null},options_error:`全局默认配置暂时无法读取：${message(error)}。请重新检查。`};}
      return ai.options;
    })();
    try {return await ai.optionsLoading;} finally {ai.optionsLoading = null;}
  }
  function settingsFromRound(round) {
    const legacy = Object.hasOwn(legacySkillNames,round?.skill_selection);
    return {skill_selection:legacy ? 'product-video-script-review' : round?.skill_selection || 'auto',
      migration_notice:legacy ? '上轮的技能选择已合并。继续修改将使用统一创作与审查技能，历史结果保持原样。' : ''};
  }
  function modelChoices() {return ai.options?.models || [];}
  function selectedModel(settings) {return settings.model ? modelChoices().find(model => model.id === settings.model) : null;}
  function allowedEfforts(settings) {return (selectedModel(settings)?.reasoning_efforts || []).filter(value => typeof value === 'string');}
  function settingsForTask(task) {
    if (!ai.settings.has(task.task_id)) ai.settings.set(task.task_id,settingsFromRound(task.rounds?.at(-1)));
    return ai.settings.get(task.task_id);
  }
  function skillAvailable(id) {
    if (['auto','none'].includes(id)) return true;
    return ai.options?.skills.some(skill => skill.id === id && skill.available);
  }
  function settingsIssue(settings) {
    if (!ai.options || ai.optionsLoading) return '正在读取全局默认配置，请稍候。';
    if (ai.options.options_error) return ai.options.options_error;
    const defaults = ai.options.default_settings || {}, selected = selectedModel(defaults);
    if (defaults.model && !selected) return '全局默认模型当前不可选，请在全局设置中更新模型。';
    if (selected?.available === false) return providerOf(selected) === 'claude' ? ai.status?.providers?.claude?.error || '全局默认模型使用的 Claude Code 尚未就绪，请在全局设置中检查。' : '全局默认模型目前不可用，请重新检查或在全局设置中更新模型。';
    if (defaults.reasoning_effort && !allowedEfforts(defaults).includes(defaults.reasoning_effort)) return '全局默认模型不支持已保存的思考强度，请在全局设置中更新。';
    if (!skillAvailable(settings.skill_selection)) return '所选技能目前不可用，请改为自动匹配或选择其他技能。';
    return '';
  }
  function settingsMarkup(prefix,settings) {
    return `<fieldset class="ai-run-settings" id="${prefix}-fieldset"><p class="ai-settings-issue" role="status" ${settingsIssue(settings) ? '' : 'hidden'}>${esc(settingsIssue(settings))}</p></fieldset>`;
  }
  const providerOf = model => model?.provider || (String(model?.id || '').startsWith('claude-') ? 'claude' : 'codex');
  function defaultsSummary() {
    if (!ai.options || ai.optionsLoading) return '正在读取全局默认配置…';
    if (ai.options.options_error) return '全局默认配置暂不可用';
    const defaults = ai.options.default_settings || {}, model = selectedModel(defaults);
    const label = model?.name || (defaults.model ? modelLabel(defaults.model) : '本机默认模型');
    const effort = defaults.reasoning_effort ? `思考强度：${effortNames[defaults.reasoning_effort] || defaults.reasoning_effort}` : '默认思考强度';
    return `使用全局默认 · ${label} · ${effort}`;
  }
  function defaultsMarkup(prefix) {
    return `<p class="ai-model-description ai-global-defaults"><span id="${prefix}-defaults-summary">${esc(defaultsSummary())}</span> <a href="${esc(pageURL('#settings'))}" target="_blank" rel="noopener" title="在新标签页打开，保留当前输入">全局设置</a></p>`;
  }
  // Model selection is now shared by the workbench, never a hidden browser override.
  try {localStorage.removeItem('case-workbench.ai.last-settings');} catch {}
  function renderSettings(prefix,settings) {
    const host = $(`#${prefix}-settings`);if (!host) return;
    host.innerHTML = settingsMarkup(prefix,settings);
    if (settings.migration_notice) {const notice = document.createElement('p');notice.className = 'ai-options-note';notice.textContent = settings.migration_notice;host.append(notice);}
  }
  function updateSettingsControls() {if (ai.modal) updateCreateControls();updateTaskControls();}
  function modelLabel(id) {
    const known = modelChoices().find(model => model.id === id)?.name;if (known) return known;
    const aliases = {'claude-default':'Claude 客户端默认','claude-opus':'Claude Opus','claude-sonnet':'Claude Sonnet','claude-haiku':'Claude Haiku'};
    if (aliases[id]) return aliases[id];
    const current = /^claude-([a-z]+)-(\d{1,2})(?:-(\d{1,2}))?(?:-(\d{8}))?(\[1m\])?$/.exec(String(id || ''));
    const legacy = /^claude-(\d{1,2})(?:-(\d{1,2}))?-([a-z]+)(?:-(\d{8}))?(\[1m\])?$/.exec(String(id || ''));
    const claude = current ? {family:current[1],major:current[2],minor:current[3],date:current[4],context:current[5]} : legacy ? {family:legacy[3],major:legacy[1],minor:legacy[2],date:legacy[4],context:legacy[5]} : null;
    if (claude) {
      const details = [claude.date ? `${claude.date.slice(0,4)}-${claude.date.slice(4,6)}-${claude.date.slice(6,8)}` : '',claude.context ? '1M 上下文' : ''].filter(Boolean);
      return `Claude ${claude.family[0].toUpperCase()}${claude.family.slice(1)} ${claude.major}${claude.minor ? '.' + claude.minor : ''}${details.length ? '（' + details.join('，') + '）' : ''}`;
    }
    const codex = /^gpt-(\d+(?:\.\d+)?)(?:-(sol|astra|luna|terra))?$/.exec(String(id || ''));
    return codex ? `GPT-${codex[1]}${codex[2] ? '-' + codex[2][0].toUpperCase() + codex[2].slice(1) : ''}` : id;
  }
  function paintService() {
    const pill = $('#ai-check-service.ai-service-pill');
    setText('#ai-service-label',pill && ai.status && !ai.status.available ? 'AI 暂不可用' : availabilityText());
    if (pill) {pill.dataset.state = !ai.status ? 'checking' : ai.status.available ? 'ok' : 'off';pill.title = `${availabilityText()}（点一下重新检查）`;}
  }
  function serviceMarkup() {return `<div class="ai-service-row"><p class="ai-service-status ${ai.status?.available ? 'available' : 'unavailable'}" id="ai-service-label">${esc(availabilityText())}</p><button type="button" class="subtle" id="ai-check-service">${icon('refresh')}重新检查</button></div>`;}
  function connectServiceButton() {$('#ai-check-service')?.addEventListener('click',async () => {await Promise.all([checkStatus(true),loadOptions(true)]);setText('#ai-service-label',availabilityText());if (ai.task) renderSettings('ai-continue',settingsForTask(ai.task));updateTaskControls();});}
  // Writing variants from a case is the top bar's 「用这个案例写稿」; the card keeps review, retrospective and history.
  async function mountCaseActions(item) {
    const panel = document.createElement('section');panel.className = 'ai-case-actions';panel.dataset.caseId = item.case_id;
    panel.innerHTML = `<div class="ai-case-heading"><div><h2>${icon('spark')}AI 协作</h2></div><div class="ai-action-buttons">${Object.entries(actions).filter(([key]) => key !== 'variants').map(([key,label]) => `<button type="button" class="secondary" data-ai-action="${key}">${esc(label)}</button>`).join('')}</div></div><details class="ai-case-history" hidden><summary>协作记录 <span class="ai-history-count"></span></summary><div class="ai-case-task-list"><p class="ai-muted">正在读取协作记录…</p></div></details>`;
    const host = $('#detail-ai-panel'), tools = $('.detail-action-row > .detail-tools');
    if (host) host.replaceChildren(panel);
    else if (tools) tools.insertAdjacentElement('beforebegin',panel); else $('.detail-tabs')?.insertAdjacentElement('beforebegin',panel);
    $$('[data-ai-action]',panel).forEach(button => button.onclick = () => openCreate(item,button.dataset.aiAction));
    try {
      const data = await api(`/api/ai/tasks?case_id=${encodeURIComponent(item.case_id)}`);
      if (!panel.isConnected) return;
      const tasks = data.tasks || [];
      $('.ai-case-history',panel).hidden = tasks.length === 0;
      $('.ai-history-count',panel).textContent = `· ${tasks.length} 条`;
      $('.ai-case-task-list',panel).innerHTML = tasks.length ? tasks.map(task => `<a class="ai-case-task" href="#ai/${encodeURIComponent(task.task_id)}"><span>${esc(actions[task.action] || 'AI 协作')} · ${esc(readableDate(task.updated_at))}</span><span class="ai-status ${esc(task.status)}">${esc(statusText(task))}</span></a>`).join('') : '<p class="ai-muted">还没有协作记录。选择上方入口开始。</p>';
    } catch (error) {if (panel.isConnected) { $('.ai-case-history',panel).hidden = false; $('.ai-case-task-list',panel).textContent = `暂时无法读取协作记录：${message(error)}`; }}
  }
  function modalOpen() {return !!ai.modal || $('#ai-delete-dialog').open;}
  function hasUnsavedInput() {return ai.deleting || !!ai.modal?.busy || !!ai.modal?.instruction?.trim() || [...ai.drafts.values()].some(value => value.trim()) || ai.pending.size > 0;}
  function conflictingModal() {return state.editor || state.deletion || state.deleteOperation || state.bulk || state.bulkOperation || state.archiveOperation || $('#ai-delete-dialog').open;}
  function createKey(item,action,options = {}) {
    const product = item.product || window.WorkbenchShell?.product() || '';
    const scope = `${item.plan_id ? `:plan:${encodeURIComponent(item.plan_id)}` : ''}${options.variantIndex !== undefined ? `:variant:${options.variantIndex}` : ''}`;
    return `create:${item.case_id || `${product}:${item.direction_id || 'blank'}`}:${action}${scope}`;
  }
  const variantIdentity = variant => JSON.stringify(['progression','variant_type','main_difference','concern','closing','execution_change'].map(key => variant?.[key] ?? ''));
  // Writing entries are labelled 「开始写稿」 everywhere, so their dialog carries the same name; the case card keeps its own action names.
  async function openCreate(item,action,heading = actions[action],options = {}) {
    if (!actions[action] || ai.modal) return;
    if (conflictingModal()) {toast('请先完成或关闭当前编辑和操作，再开始 AI 协作。',true);return;}
    const product = item.product || window.WorkbenchShell?.product() || '';
    // Each entry keeps its own uncertain submission; reopening another variant or plan must never replay that request.
    const key = createKey(item,action,options), pending = ai.pending.get(key);
    const verifyVariant = options.variantIndex !== undefined && !pending;
    const expectedVariant = verifyVariant ? variantIdentity(item.variants[options.variantIndex]) : '';
    const materialIds = pending?.payload.material_ids || item.material_ids || null;
    const modal = {key,caseId:item.case_id,product,directionId:pending?.payload.direction_id || item.direction_id || '',caseTitle:item.current?.title || item.title || item.direction_id || item.case_id || `${product} · 空白写稿`,revision:pending?.payload.revision || item.revision,planId:pending?.payload.plan_id || item.plan_id || '',action,instruction:pending ? pending.payload.instruction || '' : item.plan_note || '',includeBasket:pending ? !!(pending.payload.include_basket || pending.payload.material_ids?.length) : !!(materialIds?.length || item.direction_id),materialIds,materials:[],directions:item.direction_id ? [item] : [],variantSel:pending?.payload.variants ? pending.payload.variants.map(item => item.index) : pending ? [] : options.variantIndex !== undefined ? [options.variantIndex] : null,newVariants:pending?.payload.new_variant_count || 0,sourceMode:pending?.payload.writing_mode?.mode || 'adapt',divergeCount:pending?.payload.writing_mode?.count || 3,settings:{skill_selection:pending?.payload.skill_selection || 'auto'},busy:false,blocked:verifyVariant,error:pending ? '此入口上次提交的结果尚未确认。请重试同一请求核对，不会重复创建任务。' : ''};
    ai.modal = modal;
    setText('#ai-create-title',heading);setText('#ai-create-case',modal.caseTitle);
    setText('#ai-create-version',item.case_id ? '基于当前案例版本' : item.direction_id ? '基于当前方向定义' : '空白写稿');
    if ($('#ai-create-advanced')) $('#ai-create-advanced').open = false;
    $('#ai-create-defaults').innerHTML = defaultsMarkup('ai-create');
    if (!$('#ai-basket-choice')) $('#ai-create-settings').insertAdjacentHTML('afterend','<div id="ai-basket-choice" class="ai-basket-choice"></div>');
    renderBasketChoice(modal);
    $('#ai-create-instruction').value = modal.instruction;
    $('#ai-create-instruction').placeholder = requestHints[action];
    const optionsRequest = loadOptions(false,true);
    renderSettings('ai-create',modal.settings);$('#ai-create-dialog').showModal();updateCreateControls();
    await Promise.all([checkStatus(),optionsRequest,api(`/api/materials?product=${encodeURIComponent(product)}`).then(data => {modal.materials = data.items || [];}).catch(error => {modal.basketError = message(error);}),item.case_id ? Promise.resolve() : api(`/api/direction-catalog?product=${encodeURIComponent(product)}`).then(data => {
      modal.directions = data.directions || [];
      if (verifyVariant) {
        const current = modal.directions.find(direction => direction.direction_id === item.direction_id)?.variants?.[options.variantIndex];
        modal.blocked = !current || !!current.conflict || variantIdentity(current) !== expectedVariant;
        if (modal.blocked) modal.error = '这条变体已更新，请关闭窗口，重新读取方向后再选择。';
      }
    }).catch(() => {if (verifyVariant) modal.error = '暂时无法核对这条变体，请关闭窗口后重试。';})]);if (ai.modal !== modal) return;renderBasketChoice(modal);renderSettings('ai-create',modal.settings);updateCreateControls();$('#ai-create-instruction').focus();
  }
  function renderBasketChoice(modal) {
    const host = $('#ai-basket-choice');if (!host) return;
    // The writing mode sits above the request: it decides whether the request is needed at all.
    let modeHost = $('#ai-source-mode-host');
    if (!modeHost) {$('#ai-create-form .ai-create-ask').insertAdjacentHTML('beforebegin','<div id="ai-source-mode-host"></div>');modeHost = $('#ai-source-mode-host');}
    modeHost.innerHTML = modal.caseId && modal.action === 'variants' ? sourceModeMarkup(modal) : '';
    host.innerHTML = `${modal.action === 'variants' ? '<div class="ai-source-row"><span class="context-note">已有写好的稿件？</span><button type="button" class="subtle" id="ai-create-manual">手动录入</button></div>' : ''}${!modal.caseId ? `<div class="ai-source-row"><label class="field"><span>本次方向</span><select id="ai-create-direction"><option value="">空白写稿，不指定方向</option>${modal.directions.filter(item => item.status !== 'archived').map(item => `<option value="${esc(item.direction_id)}" ${item.direction_id === modal.directionId ? 'selected' : ''}>${esc(item.direction_id)} · ${esc(item.main_difference)}</option>`).join('')}</select></label>${!modal.materialIds ? '<button type="button" class="subtle" id="ai-create-from-case">改为从案例写稿</button>' : ''}</div>` : ''}<label class="ai-check"><input id="ai-include-basket" type="checkbox" ${modal.includeBasket ? 'checked' : ''} ${ai.pending.has(modal.key) ? 'disabled' : ''}> 携带${modal.materialIds ? '已选择的素材' : '当前产品的摘录篮'}（${modal.materialIds?.length ?? modal.materials.length} 条）</label><p class="context-note">${esc(modal.basketError ? `摘录篮暂不可用：${modal.basketError}。可取消携带素材，或关闭后重试。` : '提交时固定选中的素材；以后修改摘录篮不会改动此任务。')}</p><div id="ai-variant-plan"></div>`;
    $('#ai-include-basket').onchange = event => {modal.includeBasket = event.target.checked;updateCreateControls();};
    $$('[name="ai-source-mode"]',modeHost).forEach(radio => {radio.onchange = () => {modal.sourceMode = radio.value;$('#ai-diverge-count').disabled = modal.sourceMode !== 'diverge';updateCreateControls();};});
    if ($('#ai-diverge-count')) $('#ai-diverge-count').oninput = event => {modal.divergeCount = Math.max(1,Math.min(5,Math.floor(Number(event.target.value) || 1)));updateCreateControls();};
    if ($('#ai-create-direction')) $('#ai-create-direction').onchange = event => {modal.directionId = event.target.value;modal.variantSel = null;renderVariantPlan(modal);updateCreateControls();};
    if ($('#ai-create-from-case')) $('#ai-create-from-case').onclick = () => {if (modal.busy) return;closeCreate();void window.WorkbenchShell?.chooseWritingCase();};
    if ($('#ai-create-manual')) $('#ai-create-manual').onclick = () => {if (modal.busy || ai.pending.has(modal.key)) return;closeCreate();void window.CaseScripts?.openManual();};
    renderVariantPlan(modal);
  }
  // Writing from a case: stay close to the original, or grow new scripts from it, each on a progression of its own.
  function sourceModeMarkup(modal) {
    const diverge = modal.sourceMode === 'diverge';
    return `<fieldset class="ai-source-mode"><legend>写法</legend><label class="ai-variant-option"><input type="radio" name="ai-source-mode" value="adapt" ${diverge ? '' : 'checked'}><span><strong>贴着原稿改</strong><small>按你的要求在原稿上做变体编辑，推进结构基本不动。</small></span></label><label class="ai-variant-option"><input type="radio" name="ai-source-mode" value="diverge" ${diverge ? 'checked' : ''}><span><strong>原稿发散</strong><small>保留原稿主打卖点、开头力度和有效原话，带上拆解；每条换一种推进结构，不与原稿和已登记变体重复。</small></span></label><label class="ai-variant-new"><span>发散条数</span><input id="ai-diverge-count" type="number" min="1" max="5" step="1" value="${modal.divergeCount}" ${diverge ? '' : 'disabled'}></label></fieldset>`;
  }
  const divergeActive = modal => !!modal.caseId && modal.action === 'variants' && modal.sourceMode === 'diverge';
  // Writing from a direction: each ticked recorded variant becomes one script; new ones are only proposals.
  function planDirection(modal) {return modal.action === 'variants' && !modal.caseId && modal.directionId ? modal.directions.find(item => item.direction_id === modal.directionId && Array.isArray(item.variants)) || null : null;}
  const planVariants = direction => (direction?.variants || []).map((variant,index) => ({variant,index})).filter(({variant}) => !variant.conflict);
  function planActive(modal) {return !!planDirection(modal) && ((modal.variantSel || []).length + modal.newVariants) > 0;}
  function renderVariantPlan(modal) {
    const host = $('#ai-variant-plan');if (!host) return;
    const direction = planDirection(modal);
    if (!direction) {host.innerHTML = '';return;}
    if (modal.variantSel === null) modal.variantSel = modal.planId ? [] : planVariants(direction).map(entry => entry.index);
    const chosen = new Set(modal.variantSel), entries = planVariants(direction).sort((a,b) => Number(!!b.variant.baseline) - Number(!!a.variant.baseline) || a.index - b.index);
    const pendingNote = String(direction.notes || '').startsWith('待核') ? String(direction.notes).split('\n')[0] : '';
    host.innerHTML = `<fieldset class="ai-variant-plan"><legend>按变体写稿 · 每勾一条写一稿</legend>${pendingNote ? `<p class="ai-variant-warning">${esc(pendingNote)}</p>` : ''}${entries.length ? entries.map(({variant,index}) => `<label class="ai-variant-option"><input type="checkbox" data-variant-index="${index}" ${chosen.has(index) ? 'checked' : ''}><span><strong>#${index + 1} ${esc(variant.progression || '未标推进结构')}</strong>${variant.baseline ? '<em>基准稿</em>' : ''}<small>${esc(variant.concern || '')}</small></span></label>`).join('') : '<p class="ai-muted">这条方向还没有登记变体。</p>'}<label class="ai-variant-new"><span>另提新变体</span><input id="ai-new-variants" type="number" min="0" max="5" step="1" value="${modal.newVariants}"></label><small class="ai-muted">新变体的推进结构不与已有变体重复，结果标为“建议新增”，确认后再登记。</small></fieldset>`;
    $$('[data-variant-index]',host).forEach(box => {box.onchange = () => {modal.variantSel = $$('[data-variant-index]:checked',host).map(item => Number(item.dataset.variantIndex));updateCreateControls();};});
    $('#ai-new-variants').oninput = event => {modal.newVariants = Math.max(0,Math.min(5,Math.floor(Number(event.target.value) || 0)));updateCreateControls();};
    updateCreateControls();
  }
  async function startFromDirection(direction,options = {}) {
    if (!direction?.direction_id || !direction.product) {toast('请先选择具体方向。',true);return false;}
    const index = options.variantIndex;
    const pending = ai.pending.has(createKey(direction,'variants',options));
    if (!pending && index !== undefined && (!Number.isInteger(index) || !direction.variants?.[index] || direction.variants[index].conflict)) {toast('这条变体暂不可写稿，请重新读取方向后选择可用变体。',true);return false;}
    await openCreate({...direction,title:`${direction.direction_id} · ${direction.main_difference || '方向写稿'}${index !== undefined ? ` · 变体 ${index + 1}` : ''}`},'variants','开始写稿',options);return !!ai.modal;
  }
  async function startBlank(product = window.WorkbenchShell?.product()) {
    if (!product) {toast('请先选择一个具体产品，再开始空白写稿。',true);return false;}
    await openCreate({product,title:`${product} · 空白写稿`},'variants','开始写稿');return !!ai.modal;
  }
  async function startWithMaterials(items) {
    if (!Array.isArray(items) || !items.length) {toast('请先选择素材。',true);return false;}
    const product = items[0].product || currentProductForMaterials(items);
    if (!product || items.some(item => item.product && window.WorkbenchShell && !window.WorkbenchShell.matchesProduct(item.product,product))) {toast('请选取同一产品的素材后开始写稿。',true);return false;}
    const material_ids = items.map(item => item.id || item.material_id).filter(Boolean);
    if (material_ids.length !== items.length) {toast('素材信息不完整，请重新读取摘录篮。',true);return false;}
    await openCreate({product,title:`${product} · 素材写稿`,material_ids},'variants','开始写稿');return !!ai.modal;
  }
  function currentProductForMaterials() {return window.WorkbenchShell?.product() || '';}
  async function startWriting(caseId) {
    if (!caseId || ai.modal) return false;
    const product = window.WorkbenchShell?.product();
    try {
      const item = await api(`/api/cases/${encodeURIComponent(caseId)}`);
      if (window.WorkbenchShell && (window.WorkbenchShell.product() !== product || !window.WorkbenchShell.matchesProduct(item.product))) return false;
      await openCreate(item,'variants','开始写稿');
      return !!ai.modal;
    } catch (error) {toast(`暂时无法开始写稿：${message(error)}`,true);return false;}
  }
  function updateCreateControls() {
    const modal = ai.modal;if (!modal) return;
    const pending = ai.pending.get(modal.key), busy = modal.busy;
    setText('#ai-create-availability',availabilityText());
    setText('#ai-create-defaults-summary',defaultsSummary());
    // The folded settings open by themselves when something there needs attention.
    const issue = !pending && settingsIssue(modal.settings) && ai.options && !ai.optionsLoading;
    setText('#ai-create-settings-summary',issue ? '需要处理' : ai.status?.available === false ? 'AI 暂不可用' : '');
    $('#ai-create-settings-summary')?.classList.toggle('is-warn',!!issue || ai.status?.available === false);
    if ((issue || ai.status?.available === false) && $('#ai-create-advanced')) $('#ai-create-advanced').open = true;
    $('#ai-create-availability').classList.toggle('unavailable',ai.status?.available === false);
    $('#ai-create-instruction').readOnly = busy || !!pending;
    const plan = planActive(modal), diverge = divergeActive(modal), optional = plan || diverge, selected = (modal.variantSel || []).length;
    setText('#ai-create-instruction-label',optional ? '补充要求（选填）' : '这次希望怎么做？');
    $('#ai-create-instruction').required = !optional;
    $('#ai-create-instruction').placeholder = diverge ? '例如：开头保留原片那句；其中一条先讲旧方法的麻烦。' : plan ? '例如：控制在 40 秒内；出镜人没有用过旧装备。' : requestHints[modal.action];
    if ($('.ai-variant-plan')) $('.ai-variant-plan').disabled = busy || !!pending;
    if ($('.ai-source-mode')) $('.ai-source-mode').disabled = busy || !!pending;
    $('#close-ai-create').disabled = busy;$('#cancel-ai-create').disabled = busy;
    $('#submit-ai-create').disabled = busy || modal.blocked || (!pending && modal.includeBasket && !!modal.basketError) || !ai.status?.available || !state.token || (!pending && !modal.instruction.trim() && !optional) || (!pending && !!settingsIssue(modal.settings));
    if ($('#ai-create-fieldset')) $('#ai-create-fieldset').disabled = busy || !!pending;
    if ($('#ai-include-basket')) $('#ai-include-basket').disabled = busy || !!pending;
    if ($('#ai-create-direction')) $('#ai-create-direction').disabled = busy || !!pending || modal.blocked;
    if ($('#ai-create-manual')) $('#ai-create-manual').disabled = busy || !!pending;
    $('#ai-create-check').disabled = busy;
    setText('#submit-ai-create',busy ? '正在提交…' : pending ? '重试同一请求' : '开始协作');
    setText('#ai-create-error',modal.error);$('#ai-create-error').hidden = !modal.error;
    setText('#ai-create-hint',pending ? '为防止重复创建，重试使用上次的相同要求和请求。' : diverge ? `将以原稿为底发散 ${modal.divergeCount} 条，每条推进结构不同；提交后可在「内容创作」查看进度与结果。` : plan ? `将按 ${selected} 条变体各写一稿${modal.newVariants ? `，另提 ${modal.newVariants} 条新变体` : ''}；提交后可在「内容创作」查看进度与结果。` : '提交后可在「内容创作」查看进度与结果。');
  }
  function closeCreate() {
    if (ai.modal?.busy) {toast('正在提交，请等待返回结果。',true);return;}
    ai.modal = null;$('#ai-create-dialog').close();$('#ai-create-instruction').value = '';
  }
  async function submitCreate(event) {
    event.preventDefault();const modal = ai.modal;
    if (!modal || modal.busy || modal.blocked || !ai.status?.available || (!ai.pending.has(modal.key) && ((modal.includeBasket && !!modal.basketError) || (!modal.instruction.trim() && !planActive(modal) && !divergeActive(modal)) || settingsIssue(modal.settings)))) return;
    let pending = ai.pending.get(modal.key);
    if (!pending) {pending = {path:'/api/ai/tasks',payload:{request_id:crypto.randomUUID(),...(modal.planId ? {plan_id:modal.planId} : {}),...(modal.caseId ? {case_id:modal.caseId,revision:modal.revision} : {product:modal.product,...(modal.directionId ? {direction_id:modal.directionId} : {})}),...(modal.includeBasket && modal.materialIds ? {material_ids:modal.materialIds} : {include_basket:modal.includeBasket}),action:modal.action,instruction:modal.instruction.trim(),...planPayload(modal),...(divergeActive(modal) ? {writing_mode:{mode:'diverge',count:modal.divergeCount}} : {}),skill_selection:modal.settings.skill_selection}};ai.pending.set(modal.key,pending);}
    modal.busy = true;pending.busy = true;modal.error = '';ai.mutation++;updateCreateControls();
    try {
      const response = await api(pending.path,{method:'POST',body:pending.payload});
      if (!response.task?.task_id) throw new Error('没有收到完整任务信息，请重试同一请求核对。');
      ai.pending.delete(modal.key);modal.busy = false;ai.modal = null;$('#ai-create-dialog').close();$('#ai-create-instruction').value = '';
      ai.task = response.task;rememberTaskState(response.task);location.hash = pageURL(`#ai/${encodeURIComponent(response.task.task_id)}`);toast('任务已提交，可在这里查看处理进度。');
    } catch (error) {
      if (definitive(error)) ai.pending.delete(modal.key);
      if (error.status === 409 || error.status === 404) modal.blocked = true;
      modal.error = error.status === 409 ? `来源资料已更新，请关闭此窗口，重新打开后再发起。${message(error)}` : definitive(error) ? message(error) : `提交结果尚未确认：${message(error)}。请重试同一请求核对。`;
      if (error.status === 503) {ai.status = {available:false,error:message(error)};}
    } finally {pending.busy = false;modal.busy = false;if (ai.modal === modal) updateCreateControls();void loadTaskList(0,true).catch(() => {});}
  }
  // One task per row: kind, title, the first line of the request, then model, time and status.
  function taskCard(task) {
    const busy = active(task), title = contextTitle(task);
    const detail = task.last_error ? '原因：' + task.last_error : typeof task.progress === 'string' && busy ? task.progress : taskStarted(task) || (task.stale ? '基于案例的较早版本' : '');
    return `<div class="ai-task-entry${task.archived ? ' is-archived' : ''}" data-ai-task-id="${esc(task.task_id)}"><div class="ai-task-select" ${busy ? 'title="任务正在处理，结束后可整理"' : ''}><input type="checkbox" aria-label="选择 ${esc(title)} 的协作记录" ${busy ? 'disabled' : ''}></div><a class="ai-task-card" href="#ai/${encodeURIComponent(task.task_id)}" title="${esc(task.task_id)}"><span class="ai-action-kind">${esc(actions[task.action] || 'AI 协作')}</span><span class="ai-row-main"><strong>${esc(title)}</strong>${detail ? `<small${task.last_error ? ' class="ai-task-reason"' : ''}>${esc(detail)}</small>` : ''}<small class="ai-management-badge">${esc(managementLabel(task))}${task.archived ? ' · 已归档' : ''}${isWriting(task) && task.adopted_script_count ? ` · 已保存 ${task.adopted_script_count} 份采用稿` : ''}</small></span><span class="ai-row-meta"><span>${esc(readableDate(task.updated_at))} 更新${task.round_count > 1 ? ` · ${task.round_count} 轮` : ''}${task.stale ? ' · 案例已更新' : ''}</span></span><span class="ai-status ${esc(task.status)}">${esc(statusText(task))}</span></a></div>`;
  }
  const listFilters = {all:['全部',() => true],active:['进行中',task => active(task)],completed:['已完成',task => task.status === 'completed'],stopped:['未完成',task => ['failed','cancelled','interrupted'].includes(task.status)]};
  function startOptions() {
    return `<div class="ai-start"><h3>开始写稿</h3><p>检查候选后开始制作，采用稿和版本历史会自动保留。</p><div class="ai-start-options"><button type="button" class="ai-start-card" id="ai-start-blank"><strong>空白写稿</strong><small>只写这次的要求</small></button><button type="button" class="ai-start-card" data-ai-start="direction"><strong>从方向写稿</strong><small>选一个测试方向和它的变体</small></button><button type="button" class="ai-start-card" data-ai-start="case"><strong>从案例写稿</strong><small>基于已收录的参考案例</small></button><button type="button" class="ai-start-card" data-ai-start="manual"><strong>手动录入</strong><small>保存已经写好的采用稿</small></button></div></div>`;
  }
  function viewTasks(tasks = ai.taskList?.tasks || []) {return scopedTasks(tasks).filter(task => ai.listView === 'all' || (ai.listView === 'history' ? !!task.archived : !task.archived));}
  function visibleTasks(tasks = ai.taskList?.tasks || []) {return viewTasks(tasks).filter(listFilters[ai.filter || 'all'][1]).filter(task => !ai.managementFilter || managementState(task) === ai.managementFilter);}
  function selectableTasks(tasks = visibleTasks()) {return scopedTasks(tasks).filter(task => !active(task));}
  function updateListSelection(tasks = visibleTasks()) {
    tasks = scopedTasks(tasks);
    const available = selectableTasks(tasks), ids = new Set(available.map(task => task.task_id));
    for (const id of ai.selected) if (!ids.has(id)) ai.selected.delete(id);
    const all = $('#ai-select-all');if (!all) return;
    const count = ai.selected.size;
    all.disabled = !available.length || ai.deleting || ai.managing;
    all.checked = !!available.length && count === available.length;
    all.indeterminate = count > 0 && count < available.length;
    setText('#ai-selected-count',`已选 ${count} 条`);
    $('#ai-delete-selected').disabled = !count || ai.deleting || ai.managing;
    const selected = available.filter(task => ai.selected.has(task.task_id));
    const archive = $('#ai-archive-selected'), restore = $('#ai-restore-selected');
    if (archive) {const retry = ai.pending.has('manage:list-archive');archive.hidden = ai.listView === 'history' && !retry;archive.disabled = (!retry && !selected.some(task => !task.archived)) || ai.managing || ai.deleting;archive.textContent = retry ? '重试归档' : '归档所选';}
    if (restore) {const retry = ai.pending.has('manage:list-restore');restore.hidden = ai.listView === 'active' && !retry;restore.disabled = (!retry && !selected.some(task => task.archived)) || ai.managing || ai.deleting;restore.textContent = retry ? '重试恢复' : '恢复所选';}
    setText('#ai-selection-note',tasks.some(active) ? '处理中或停止中的任务结束后可归档。' : '归档后保留回答与关联，可在历史中恢复。');
    $$('.ai-task-entry', $('#ai-task-list')).forEach(entry => {
      const checked = ai.selected.has(entry.dataset.aiTaskId), checkbox = $('input[type="checkbox"]',entry);
      entry.classList.toggle('is-selected',checked);checkbox.checked = checked;
    });
  }
  async function manageTasks(key,items,patch) {
    const pendingKey = `manage:${key}`;
    let pending = ai.pending.get(pendingKey);
    if (ai.managing || ai.deleting || (!pending && !items.length)) return;
    if (!pending) {pending = {payload:{request_id:crypto.randomUUID(),items:items.map(task => ({task_id:task.task_id,revision:task.revision})),...patch}};ai.pending.set(pendingKey,pending);}
    ai.managing = true;ai.mutation++;updateListSelection();updateTaskControls();
    try {
      const result = await api('/api/ai/tasks/manage',{method:'POST',body:pending.payload});
      ai.pending.delete(pendingKey);
      for (const task of result.updated || []) {rememberTaskState(task);ai.selected.delete(task.task_id);if (ai.task?.task_id === task.task_id && taskRouteId() === task.task_id) updateTask(task);}
      ai.taskListAt = 0;try {await loadTaskList(0,true);} catch {}
      if (pageHash() === '#ai') {ai.lastList = '';await renderList(true,ai.sequence);}
      const failures = result.failed || [];
      if (failures.length) toast(failures.map(item => item.error || '任务整理未完成').join('；'),true);
      else toast(pending.payload.archived === true ? `已归档 ${result.updated?.length || 0} 条，可在历史中恢复。` : pending.payload.archived === false ? `已恢复 ${result.updated?.length || 0} 条。` : '管理状态已保存。');
    } catch (error) {
      if (definitive(error)) ai.pending.delete(pendingKey);
      const reason = definitive(error) ? message(error) : `整理结果尚未确认：${message(error)}。再次点击会核对同一请求。`;
      toast(reason,true);
      if (error.status === 409) {ai.taskListAt = 0;void loadTaskList(0,true).catch(() => {});if (ai.task) void pollTask(true);}
    } finally {ai.managing = false;updateListSelection();updateTaskControls();}
  }
  function manageSelected(archived) {const items = selectableTasks().filter(task => ai.selected.has(task.task_id) && !!task.archived !== archived);return manageTasks(archived ? 'list-archive' : 'list-restore',items,{archived});}
  function openDeleteDialog() {
    if (ai.deleting) return;
    ai.deleteSelection = selectableTasks().filter(task => ai.selected.has(task.task_id)).map(task => ({task_id:task.task_id,revision:task.revision}));
    if (!ai.deleteSelection.length) return;
    setText('#ai-delete-count',`将删除 ${ai.deleteSelection.length} 条本地协作记录。`);
    setText('#ai-delete-error','');$('#ai-delete-error').hidden = true;
    $('#confirm-ai-delete').disabled = false;
    $('#ai-delete-dialog').showModal();
  }
  function closeDeleteDialog() {if (!ai.deleting) {$('#ai-delete-dialog').close();ai.deleteSelection = [];}}
  async function submitDelete(event) {
    event.preventDefault();if (ai.deleting || !ai.deleteSelection.length) return;
    ai.deleting = true;ai.mutation++;
    $('#confirm-ai-delete').disabled = true;$('#cancel-ai-delete').disabled = true;$('#close-ai-delete').disabled = true;
    setText('#confirm-ai-delete','正在删除…');setText('#ai-delete-error','');$('#ai-delete-error').hidden = true;
    const requested = [...ai.deleteSelection];
    try {
      const result = await api('/api/ai/tasks/delete',{method:'POST',body:{tasks:requested}});
      const deleted = new Set(Array.isArray(result.deleted) ? result.deleted : []);
      for (const id of deleted) ai.selected.delete(id);
      ai.deleteSelection = requested.filter(task => !deleted.has(task.task_id));
      try {ai.taskListAt = 0;await loadTaskList(0,true);} catch {}
      if (ai.taskList) {ai.taskList.tasks = (ai.taskList.tasks || []).filter(task => !deleted.has(task.task_id));ai.taskListAt = Date.now();updateNavCount(ai.taskList.tasks);}
      if (pageHash() === '#ai') {ai.lastList = '';await renderList(true,ai.sequence);}
      if (!ai.deleteSelection.length) {$('#ai-delete-dialog').close();toast(`已删除 ${deleted.size} 条本地协作记录。`);}
      else {
        const failures = Array.isArray(result.failed) ? result.failed : [];
        setText('#ai-delete-count',`仍有 ${ai.deleteSelection.length} 条未删除。`);
        const titles = new Map((ai.taskList?.tasks || []).map(task => [task.task_id,task.case_title || task.case_id || task.task_id]));
        setText('#ai-delete-error',failures.map(item => `${titles.get(item.task_id) || item.task_id}：${item.error || '删除失败'}`).join('；') || '部分记录未删除，请重新检查后重试。');
        $('#ai-delete-error').hidden = false;
        if (deleted.size) toast(`已删除 ${deleted.size} 条本地协作记录。`);
      }
    } catch (error) {
      setText('#ai-delete-error',error.status === 404 ? '当前工作台服务尚未加载删除功能，请重启工作台并刷新页面。' : `删除结果暂未确认：${message(error)} 请重新检查列表后重试。`);$('#ai-delete-error').hidden = false;
      if (error.status !== 404) {try {ai.taskListAt = 0;await loadTaskList(0,true);if (pageHash() === '#ai') {ai.lastList = '';void renderList(true,ai.sequence);}} catch {}}
      ai.deleteSelection = requested.filter(task => ai.taskList?.tasks?.some(current => current.task_id === task.task_id) ?? true);
      if (ai.deleteSelection.length) setText('#ai-delete-count',`仍有 ${ai.deleteSelection.length} 条本地协作记录待核对。`);
      else {$('#ai-delete-dialog').close();toast('所选记录已不在本地列表中。');}
    } finally {
      ai.deleting = false;$('#confirm-ai-delete').disabled = !ai.deleteSelection.length;
      $('#cancel-ai-delete').disabled = false;$('#close-ai-delete').disabled = false;
      $('#confirm-ai-delete').innerHTML = `${icon('trash')}删除本地记录`;updateListSelection();
    }
  }
  function captureListState() {
    const list = $('#ai-task-list'), grouping = $('#ai-group-by'), selectedFilter = $('[data-ai-filter][aria-pressed="true"]');
    if (ai.page !== 'list' || !list?.isConnected || !grouping?.isConnected || !selectedFilter?.isConnected) return null;
    return {filter:selectedFilter.dataset.aiFilter,groupBy:grouping.value,view:ai.listView,management:ai.managementFilter,product:ai.listProduct};
  }
  function restoreListState(saved) {
    ai.filter = saved && Object.hasOwn(listFilters,saved.filter) ? saved.filter : 'all';
    ai.listView = saved && Object.hasOwn(listViews,saved.view) ? saved.view : 'active';
    ai.managementFilter = saved && Object.hasOwn(managementStates,saved.management) ? saved.management : '';
    ai.lastList = '';ai.selected.clear();$('#content').classList.remove('ai-selecting');
    const grouping = $('#ai-group-by');if (grouping) grouping.value = saved?.groupBy === 'case' ? 'case' : 'direction';
    $$('[data-ai-filter]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.aiFilter === ai.filter)));
    const toggle = $('#ai-select-toggle');if (toggle) {toggle.setAttribute('aria-pressed','false');toggle.textContent = '选择';}
    const toolbar = $('#ai-selection-toolbar');if (toolbar) toolbar.hidden = true;
    if ($('#ai-list-view')) $('#ai-list-view').value = ai.listView;
    if ($('#ai-management-filter')) $('#ai-management-filter').value = ai.managementFilter;
  }
  async function renderList(keepScroll,sequence) {
    if (ai.page !== 'list' || !$('#ai-task-list')) {
      const saved = window.WorkbenchShell?.listState?.('ai',window.WorkbenchShell?.product() || '');
      restoreListState(saved);
      ai.page = 'list';ai.task = null;ai.lastList = '';
      ai.listProduct = window.WorkbenchShell?.product() || '';
      $('#breadcrumb').innerHTML = '协作记录';
      $('#content').innerHTML = `${window.ContentFlow?.tabs?.('ai') || `<a class="back-link" href="${esc(pageURL('#content'))}">${icon('back')}返回内容列表</a>`}<div class="page-heading ai-list-heading"><div><h1>协作记录</h1><p>查看历次协作、各轮回答及采用记录，归档后仍可回看和恢复。</p></div><button type="button" class="ai-service-pill" id="ai-check-service" title="重新检查 AI 服务"><i></i><span id="ai-service-label">${esc(availabilityText())}</span></button></div>
        <div class="ai-management-toolbar"><label>查看<select id="ai-list-view">${Object.entries(listViews).map(([key,label]) => `<option value="${key}" ${ai.listView === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label>管理状态<select id="ai-management-filter"><option value="">全部管理状态</option>${Object.entries(managementFilterLabels).map(([key,label]) => `<option value="${key}" ${ai.managementFilter === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div>
        <div class="ai-list-toolbar"><div class="ai-segment" role="group" aria-label="按执行状态筛选">${Object.entries(listFilters).map(([key,[label]]) => `<button type="button" data-ai-filter="${key}" aria-pressed="${(ai.filter || 'all') === key}">${label}<small data-ai-filter-count="${key}"></small></button>`).join('')}</div><label class="ai-group-choice"><span>分组</span><select id="ai-group-by"><option value="direction">按方向</option><option value="case">按案例</option></select></label><button type="button" class="subtle" id="ai-select-toggle" aria-pressed="false">选择</button></div>
        <div class="ai-selection-toolbar" id="ai-selection-toolbar" hidden><div class="ai-selection-main"><label class="select-all"><input type="checkbox" id="ai-select-all">全选</label><span id="ai-selected-count" class="selection-count">已选 0 条</span><span id="ai-selection-note" class="selection-count"></span></div><div class="ai-selection-actions"><button type="button" class="secondary compact" id="ai-archive-selected" disabled>归档所选</button><button type="button" class="secondary compact" id="ai-restore-selected" disabled hidden>恢复所选</button><button type="button" class="danger-outline" id="ai-delete-selected" disabled>${icon('trash')}永久删除</button></div></div><div id="ai-list-error" class="ai-error" role="alert" hidden></div><div id="ai-task-list">${skeletonMarkup(2)}</div>`;
      $('#ai-group-by').value = saved?.groupBy === 'case' ? 'case' : 'direction';
      $('#ai-check-service').onclick = async () => {await Promise.all([checkStatus(true),loadOptions(true)]);paintService();};
      $('#ai-group-by').onchange = () => {ai.lastList = '';void renderList(true,ai.sequence);};
      $('#ai-list-view').onchange = event => {ai.listView = event.target.value;ai.selected.clear();ai.lastList = '';void renderList(true,ai.sequence);};
      $('#ai-management-filter').onchange = event => {ai.managementFilter = event.target.value;ai.selected.clear();ai.lastList = '';void renderList(true,ai.sequence);};
      $$('[data-ai-filter]').forEach(button => button.onclick = () => {ai.filter = button.dataset.aiFilter;$$('[data-ai-filter]').forEach(item => item.setAttribute('aria-pressed',String(item === button)));ai.lastList = '';void renderList(true,ai.sequence);});
      $('#ai-select-toggle').onclick = event => {
        const on = event.currentTarget.getAttribute('aria-pressed') !== 'true';
        event.currentTarget.setAttribute('aria-pressed',String(on));event.currentTarget.textContent = on ? '完成' : '选择';
        $('#content').classList.toggle('ai-selecting',on);$('#ai-selection-toolbar').hidden = !on;
        if (!on) {ai.selected.clear();updateListSelection();}
      };
      $('#ai-select-all').addEventListener('change',event => {const tasks = selectableTasks();ai.selected = new Set(event.target.checked ? tasks.map(task => task.task_id) : []);updateListSelection();});
      $('#ai-task-list').addEventListener('change',event => {const input = event.target;if (!input.matches('.ai-task-select input')) return;const id = input.closest('.ai-task-entry')?.dataset.aiTaskId;if (!id) return;if (input.checked) ai.selected.add(id);else ai.selected.delete(id);updateListSelection();});
      $('#ai-task-list').addEventListener('click',event => {
        if (event.target.closest('#ai-start-blank')) {void startBlank();return;}
        const start = event.target.closest('[data-ai-start]');
        if (start?.dataset.aiStart === 'direction') void startBlank().then(opened => {if (opened) setTimeout(() => $('#ai-create-direction')?.focus(),300);});
        else if (start?.dataset.aiStart === 'case') void window.WorkbenchShell?.chooseWritingCase();
        else if (start?.dataset.aiStart === 'manual') void window.CaseScripts?.openManual();
      });
      $('#ai-delete-selected').onclick = openDeleteDialog;
      $('#ai-archive-selected').onclick = () => manageSelected(true);
      $('#ai-restore-selected').onclick = () => manageSelected(false);
      if (!keepScroll) window.scrollTo(0,0);
    }
    try {
      // Client discovery can take seconds. Saved writing records are usable
      // immediately while the service indicator updates independently.
      void checkStatus().then(() => {
        if (sequence === ai.sequence && pageHash() === '#ai') paintService();
      });
      const data = await loadTaskList(2500);
      if (sequence !== ai.sequence || pageHash() !== '#ai') return;
      paintService();const all = viewTasks(data.tasks || []), tasks = visibleTasks(data.tasks || []);
      for (const [key,[,keep]] of Object.entries(listFilters)) setText(`[data-ai-filter-count="${key}"]`,key === 'all' ? '' : String(all.filter(keep).length || ''));
      updateNavCount(data.tasks || []);
      const html = !all.length ? ai.listView === 'history' ? '<div class="empty"><h3>还没有归档的任务</h3><p>归档后会保留各轮回答，可随时恢复。</p></div>' : startOptions() : !tasks.length ? '<div class="empty"><h3>没有符合筛选的任务</h3><p>换一个状态看看。</p></div>' : groupedTaskMarkup(tasks,$('#ai-group-by')?.value || 'direction');
      if (html !== ai.lastList) {$('#ai-task-list').innerHTML = html;ai.lastList = html;}
      updateListSelection(tasks);
      $('#ai-list-error').hidden = true;
    } catch (error) {if (sequence === ai.sequence && $('#ai-list-error')) {setText('#ai-list-error',`暂时无法读取任务：${message(error)}`);$('#ai-list-error').hidden = false;}}
  }
  function groupedTaskMarkup(tasks,by) {
    const groups = new Map();
    for (const task of tasks) {
      const caseItem = (state.cases || []).find(item => item.case_id === task.case_id);
      const direction = task.context?.direction_id || task.direction_id || caseItem?.resolved_direction?.direction_id || caseItem?.current?.test_direction?.direction_id;
      const identity = by === 'case' ? task.case_id || '' : direction || '';
      const label = by === 'case' ? task.case_title || task.case_id || '独立创作' : direction || '未指定方向';
      const normalizedProduct = window.WorkbenchShell?.normalizeProduct?.(taskProduct(task)) || taskProduct(task);
      const key = `${normalizedProduct}:${identity}`;
      if (!groups.has(key)) groups.set(key,{label,product:normalizedProduct,tasks:[]});
      groups.get(key).tasks.push(task);
    }
    return [...groups.values()].map(group => `<section class="ai-task-group"><h2 class="ai-group-heading">${esc(group.label)} <small>${esc(group.product)} · ${group.tasks.length} 项</small></h2><div class="ai-task-rows">${group.tasks.map(taskCard).join('')}</div></section>`).join('');
  }
  // Whether this round was shown a digest of the user-review library (writing and review rounds on a product it covers).
  function reviewContextText(context) {
    if (!context) return '';
    if (context.status === 'attached') return `已附用户评论库摘要（${context.reviews || '?'} 条评论，同步于 ${String(context.synced_at || '未记录').replace('T',' ')}）`;
    if (context.status === 'unavailable') return `用户评论库摘要未能加载：${context.reason || '未记录原因'}`;
    return context.reason || '本轮未附用户评论库摘要。';
  }
  function roundMetadata(round) {
    if (round.kind === 'external_import') return '<p class="ai-options-note">已完成稿件从对话导入，保留完整意群表与执行说明；本次导入未重新生成或复核内容。</p>';
    const skills = Array.isArray(round.skills) ? round.skills : [];
    const unavailable = Array.isArray(round.skill_unavailable) ? round.skill_unavailable : [];
    const unavailableText = unavailable.map(value => typeof value === 'string' ? value : `${value.label || value.name || value.id || '技能'}${value.reason || value.error ? `：${value.reason || value.error}` : ''}`).filter(Boolean).join('；');
    if (!round.model && !round.requested_model && !round.reasoning_effort && !round.requested_reasoning_effort && !round.skill_selection && !round.skills_status && !skills.length && !unavailable.length) return '<p class="ai-legacy-info">旧记录未保存模型/技能执行信息</p>';
    const actual = round.model ? modelLabel(round.model) : '尚未回报';
    const effort = round.reasoning_effort ? (effortNames[round.reasoning_effort] || round.reasoning_effort) : '尚未回报';
    const requested = round.requested_model ? modelLabel(round.requested_model) : '本机默认';
    const names = skills.map(skill => skill.label || skill.name).filter(Boolean).join('、');
    const skillState = round.skills_status === 'unavailable' ? `未加载技能${unavailableText ? `：${unavailableText}` : '：技能资料当前不可用。'}` : round.skill_selection === 'none' ? '未使用技能' : round.skills_status === 'submitted' ? `已提交技能资料${names ? `：${names}` : ''}` : round.skills_status === 'prepared' ? `已准备技能资料${names ? `：${names}` : ''}，等待提交` : `技能选择：${skillNames[round.skill_selection] || legacySkillNames[round.skill_selection] || '未记录'}，等待执行记录`;
    const line = [actual,effort === '尚未回报' ? '' : `思考强度：${effort}`,round.skill_selection === 'none' ? '未用技能' : names ? `技能：${names}` : '',round.review_context?.status === 'attached' ? `附评论摘要 ${round.review_context.reviews || '?'} 条` : ''].filter(Boolean).join(' · ');
    return `<p class="ai-run-line">${esc(line)}</p>${round.skills_status === 'unavailable' || round.review_context?.status === 'unavailable' ? `<p class="ai-skill-status">${esc(round.skills_status === 'unavailable' ? skillState : reviewContextText(round.review_context))}</p>` : ''}${round.migration_notice ? `<p class="ai-options-note">${esc(round.migration_notice)}</p>` : ''}<details class="ai-execution-details"><summary>本轮设置</summary><p>${esc(skillState)}</p>${round.review_context ? `<p>${esc(reviewContextText(round.review_context))}</p>` : ''}<p>请求模型：${esc(requested)} · 请求思考强度：${esc(round.requested_reasoning_effort ? effortNames[round.requested_reasoning_effort] || round.requested_reasoning_effort : '默认')}</p>${skills.map(skill => `<div><strong>${esc(skill.label || skill.name)}</strong><code>${esc(skill.path || '未记录路径')}</code>${skill.sha256 ? `<small>文件校验：${esc(skill.sha256)}</small>` : ''}</div>`).join('')}</details>${stageMetadata(round)}`;
  }
  function stageMetadata(round) {
    const check = round.format_check?.status && round.format_check.status !== 'ok' && round.format_check.reason ? `<p class="ai-format-check ${esc(round.format_check.status)}">${esc(round.format_check.reason)}</p>` : '';
    if (!round.stages) return check ? `<div class="ai-stage-status">${check}</div>` : '';
    const rows = ['draft','review','format'].filter(name => round.stages[name]).map(name => {
      const stage = round.stages[name], label = {draft:'初稿',review:'独立内容复核',format:'格式整理（只改排版）'}[name];
      const recovered = round.status !== 'completed' && !active(round) ? stage.output || (stage.messages || []).filter(entry => entry.phase !== 'commentary').map(entry => entry.text || '').join('\n\n') : '';
      return `<p>${label}：${esc(statusText(stage))} · 模型：${esc(stage.model ? modelLabel(stage.model) : '尚未回报')} · 思考强度：${esc(effortNames[stage.reasoning_effort] || stage.reasoning_effort || '尚未回报')}</p>${recovered ? `<details class="ai-execution-details"><summary>查看保留的${label}文本（本轮未完成）</summary><pre>${esc(recovered)}</pre></details>` : ''}`;
    }).join('');
    const body = `${rows}${round.stages.review ? '<small>内容复核采用独立模型调用，不代表本土达人认证、母语者审核或市场验证。</small>' : ''}`;
    return round.status === 'completed' && !check && Object.values(round.stages).every(stage => stage.status === 'completed') ? `<details class="ai-execution-details"><summary>执行阶段与复核记录</summary><div class="ai-stage-status">${body}</div></details>` : `<div class="ai-stage-status">${body}${check}</div>`;
  }
  function responseLabel(round,entry) {
    if (entry.phase === 'commentary') return '处理进展';
    if (entry.phase !== 'final_answer') return '实时回复（未完成）';
    if (round.status === 'completed' && entry.complete) return resultName(ai.task);
    return ['failed','cancelled','interrupted'].includes(round.status) ? '未完成的回答' : '回答生成中';
  }
  function roundCopyText(round) {
    if (round.status === 'completed' && typeof round.output === 'string' && round.output.trim()) return round.output;
    const entries = (round.messages || []).filter(entry => entry.phase !== 'commentary' && entry.text);
    return entries.length ? entries.map(entry => entry.text).join('\n\n') : round.output || '';
  }
  function displayResponseText(text, extractTitles = false) {
    const labels = {main_difference:'主打差异',concern:'观众顾虑',closing:'收口',variant_type:'变体类型',execution_change:'执行改动'};
    let shown = String(text || '').replace(/\b(main_difference|concern|closing|variant_type|execution_change)\b(?=["'`*\s]*[:：=|])/g,key => labels[key]);
    // A retrospective draft's card block is drawn above the analysis, not shown as code.
    shown = shown.replace(/```retro-card\s*\{[\s\S]*?\}\s*```\s*/g,'');
    if (extractTitles) {
      const title = line => /^🔴/.test(line.trim().replace(/^[-*]\s+/,'').replace(/^\d+[.)、]\s*/,''));
      shown = shown.replace(/```[^\n]*\n([\s\S]*?)```/g,(block,body) => body.trim() && body.split(/\r?\n/).filter(line => line.trim()).every(title) ? '' : block);
      shown = shown.split(/\r?\n/).filter(line => !title(line)).join('\n');
    }
    return shown;
  }
  function writeResponseBody(body,text,settled,extractTitles = false) {
    const mode = settled ? 'markdown' : 'live';
    if ((body.dataset.source === text && body.dataset.mode === mode) || selectionInside(body)) return;
    body.innerHTML = renderMarkdown(settled ? displayResponseText(text,extractTitles) : text);
    body.dataset.source = text;body.dataset.mode = mode;
    body.classList.toggle('ai-streaming-text',!settled);
  }
  // 🔴Title#tags lines: one-tap copy per line, grouped under the script heading they follow.
  function titleLineGroups(text) {
    const groups = [];let heading = '';
    for (const raw of String(text || '').split(/\r?\n/)) {
      const line = raw.trim().replace(/^[-*]\s+/,'').replace(/^\d+[.)、]\s*/,'');
      const head = /^#{1,4}\s+(.+)$/.exec(raw.trim());
      if (head) {heading = head[1].replace(/[*_`]/g,'').trim();continue;}
      if (!line.startsWith('🔴')) continue;
      const clean = line.replace(/\*\*/g,'').replace(/`/g,'').trim();
      let group = groups.at(-1);
      if (!group || group.heading !== heading) {group = {heading,lines:[]};groups.push(group);}
      group.lines.push(clean);
    }
    return groups;
  }
  function updateTitleLines(node,text,settled) {
    let panel = $('.ai-title-lines',node);
    const groups = settled ? titleLineGroups(text) : [], total = groups.reduce((sum,group) => sum + group.lines.length,0);
    if (!total) {if (panel) panel.hidden = true;return;}
    const source = JSON.stringify(groups);
    if (!panel) {panel = document.createElement('section');panel.className = 'ai-title-lines';$('.ai-round-output',node).after(panel);}
    panel.hidden = false;
    if (panel.dataset.source === source) return;
    panel.dataset.source = source;
    panel.innerHTML = `<div class="ai-title-lines-head"><h3>发布文案 <small>${total} 条</small></h3><button type="button" class="subtle" data-copy-all-titles>${icon('copy')}复制全部</button></div><details class="ai-title-lines-detail"><summary>逐条查看与复制</summary>${groups.map(group => `<div class="ai-title-group">${group.heading ? `<h4>${esc(group.heading)}</h4>` : ''}${group.lines.map(line => `<button type="button" class="ai-title-line" data-title-line="${esc(line)}" title="复制这一行"><span>${esc(line)}</span>${icon('copy')}</button>`).join('')}</div>`).join('')}</details>`;
    panel.onclick = event => {
      const line = event.target.closest('[data-title-line]');
      if (line) {copyText(line.dataset.titleLine,'标题行已复制');line.classList.add('copied');setTimeout(() => line.classList.remove('copied'),1200);return;}
      if (event.target.closest('[data-copy-all-titles]')) copyText(groups.flatMap(group => group.lines).join('\n'),'全部标题行已复制');
    };
  }
  // Read time is counted by the workbench from each "口播（配音用）" block, never taken from the model.
  function updateReadEstimates(node,round) {
    const items = Array.isArray(round.read_estimates) ? round.read_estimates : [];
    let line = $('.ai-read-estimates',node);
    if (!items.length) {if (line) line.hidden = true;return;}
    if (!line) {line = document.createElement('p');line.className = 'ai-read-estimates';$('.ai-output-heading',node).after(line);}
    line.hidden = false;
    line.textContent = `估读：${items.map(item => `${item.label} 约 ${item.seconds} 秒（${item.words} 词）`).join(' · ')}`;
    line.title = '工作台按每分钟 150 词计算';
  }
  function updateRoundMessages(node,round) {
    const container = $('.ai-round-messages',node), entries = Array.isArray(round.messages) ? round.messages : [];
    const canonical = round.status === 'completed' && typeof round.output === 'string' && !!round.output.trim();
    const completeAnswers = entries.filter(entry => entry.phase === 'final_answer' && entry.complete && entry.text);
    // The final canonical answer is the reading copy; streamed pieces remain in raw output/history.
    const showCanonical = canonical;
    entries.forEach((entry,index) => {
      const key = String(entry.item_id || `message-${index}`);
      let block = [...container.children].find(child => child.dataset.itemId === key);
      if (!block) {block = document.createElement('section');block.dataset.itemId = key;block.innerHTML = '<h3 class="ai-message-label"></h3><div class="ai-stream-body markdown-body"></div>';container.append(block);}
      const duplicate = canonical;
      block.hidden = duplicate && !selectionInside(block);
      block.className = `ai-stream-message ${entry.phase === 'commentary' ? 'commentary' : entry.phase === 'final_answer' ? 'answer' : 'unknown'}`;
      const label = $('.ai-message-label',block), value = responseLabel(round,entry);if (label.textContent !== value) label.textContent = value;
      const settled = entry.phase === 'commentary' ? !!entry.complete : entry.phase === 'final_answer' && round.status === 'completed' && !!entry.complete;
      writeResponseBody($('.ai-stream-body',block),String(entry.text || ''),settled,settled && entry.phase === 'final_answer');
    });
    const output = $('.ai-round-output',node), hasAnswerMessages = entries.some(entry => entry.phase !== 'commentary' && entry.text);
    output.hidden = !showCanonical && hasAnswerMessages;
    if (showCanonical || !hasAnswerMessages) {
      const recovered = Object.values(round.stages || {}).some(stage => stage.output || stage.messages?.some(entry => entry.phase !== 'commentary' && entry.text));
      const text = round.output || (active(round) ? round.stages ? round.active_stage === 'format' ? '正在整理成可制作格式（只改排版，完成后逐字核对）。' : round.active_stage === 'review' ? '初稿已保存，正在独立复核。完成后显示修订候选。' : '正在起草。独立复核完成后显示修订候选。' : '正在等待回复…' : round.kind === 'format' ? '格式整理没有通过，原候选保持不变；可以在原候选里手动拆分口播。' : recovered ? '本轮未完成，已保留的文本可在执行记录中查看。' : '本轮没有返回可用回答。');
      writeResponseBody(output,text,round.status === 'completed' && !!round.output,canonical && isWriting(ai.task));
    }
    const copy = $('.ai-copy-round',node), text = roundCopyText(round);
    updateTitleLines(node,text,round.status === 'completed' && isWriting(ai.task));
    if (isWriting(ai.task)) updateReadEstimates(node,round);
    let raw = $('.ai-raw-output',node);
    if (!raw) {raw = document.createElement('details');raw.className = 'ai-raw-output ai-execution-details';raw.innerHTML = '<summary>查看原始回复与编号</summary><pre></pre>';node.querySelector('.ai-round-body').append(raw);}
    raw.hidden = !canonical;
    if (canonical && raw.querySelector('pre').textContent !== text) raw.querySelector('pre').textContent = text;
    copy.hidden = !text;
    const final = canonical || (round.status === 'completed' && completeAnswers.length > 0);
    const title = $('.ai-output-title',node);if (title) title.textContent = final ? resultName(ai.task) : active(round) ? '实时回复' : '本轮回复（未完成）';
    copy.innerHTML = icon('copy') + (final ? '复制完整原文' : '复制当前文本');copy.onclick = () => copyText(text,final ? '完整原文已复制' : '当前回复文本已复制');
    let adoption = $('.ai-candidate-actions',node);
    if (!adoption) {adoption = document.createElement('div');adoption.className = 'ai-candidate-actions';adoption.innerHTML = `<button type="button" class="primary compact" data-start-production>开始制作</button><details class="ai-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" data-adopt-script>保存采用稿</button><button type="button" role="menuitem" data-write-case>写回案例</button><button type="button" role="menuitem" data-format-round>整理为可制作格式</button></div></details>`;$('.ai-output-actions',node).prepend(adoption);}
    adoption.hidden = !canonical || !isWriting(ai.task);
    $('[data-start-production]',adoption).onclick = () => window.CaseProduction?.fromCandidate(ai.task,round);
    $('[data-adopt-script]',adoption).onclick = () => window.CaseScripts?.adoptCandidate(ai.task,round,false);
    $('[data-write-case]',adoption).onclick = () => window.CaseScripts?.adoptCandidate(ai.task,round,true);
    const relayout = $('[data-format-round]',adoption);if (relayout) {relayout.hidden = ai.task.action !== 'variants' || round.format_check?.status === 'ok';relayout.onclick = async () => {relayout.disabled = true;try {await formatRound(ai.task,round);} catch (error) {toast(message(error),true);} finally {relayout.disabled = false;}};}
  }
  function experienceContextMarkup(task) {
    const items = task.context?.review_experiences;
    if (!Array.isArray(items)) return ''; // Older tasks never selected from the experience library.
    const confidence = {insufficient:'证据不足',preliminary:'初步线索',supported:'有证据支持'};
    const referenceValues = {none:'无参考价值',weak:'弱参考价值',medium:'中等参考价值',high:'高参考价值'};
    const rows = items.map(item => {
      const source = item.source || {}, review = source.review || {}, video = source.video || {};
      const videoId = review.video_id || video.video_id;
      const hash = videoId ? `#videos?video=${encodeURIComponent(videoId)}${review.window_end ? '&feedback_window='+encodeURIComponent(review.window_end) : ''}` : '';
      const href = hash && (window.WorkbenchShell?.url(hash,review.product || taskProduct(task)) || hash);
      return `<section><p><strong>${item.kind === 'success' ? '成功做法' : '失败教训'}</strong> · ${esc(item.practice)}</p>${href ? `<p><a href="${esc(href)}">来源复盘</a></p>` : ''}<details class="ai-experience-basis"><summary>依据</summary><p>参考价值：${esc(referenceValues[item.reference_value] || '待分类')}（发起时分类）</p><p>适用条件：${esc(item.conditions || '未补充，使用前需核对')}</p><p>证据补充：${esc(item.evidence_note || '未补充')}</p><p>${esc(confidence[item.confidence] || '未标记证据把握')} · 引用范围：${item.scope === 'direction' ? `方向 ${esc(item.direction_id)}` : '本产品'}${item.dimension ? ` · ${esc(item.dimension)}` : ''}</p>${review.window_end ? `<p>观察窗到 ${esc(review.window_end)}</p>` : ''}<p>发起时版本 ${esc(item.version)}</p></details></section>`;
    }).join('');
    return `<details class="ai-version-details ai-experience-details"><summary>参考经验 · ${items.length} 条</summary><div>${rows || '<p>本次没有匹配到可带入的经验。</p>'}</div></details>`;
  }
  function taskContextMarkup(task) {
    const context = task.context || {};
    const source = context.source_review || context.review_video;
    const sourceHash = source?.video_id ? `#videos?video=${encodeURIComponent(source.video_id)}${source.window_end ? '&feedback_window='+encodeURIComponent(source.window_end) : ''}` : '';
    const link = sourceHash ? `<a href="${esc(window.WorkbenchShell?.url(sourceHash,source.product || taskProduct(task)) || sourceHash)}">回来源视频与复盘</a>` : task.case_id ? `<a href="${esc(pageURL(`#case/${encodeURIComponent(task.case_id)}`))}">${icon('file')}查看关联案例</a>` : context.direction_id ? `<a href="${esc(window.WorkbenchShell?.url(`#directions/${encodeURIComponent(context.direction_id)}`,taskProduct(task)) || '#directions')}">查看来源方向</a>` : `<span>${esc(taskProduct(task))} · ${isWriting(task) ? '空白写稿' : resultName(task)}</span>`;
    return `${link}<span>基于发起时固定的${task.case_id ? '案例版本' : context.direction_id ? '方向资料' : '创作要求'}</span>${context.writing_mode?.mode === 'diverge' ? `<span>原稿发散 · ${esc(context.writing_mode.count)} 条</span>` : ''}<details class="ai-version-details"><summary>查看本次资料</summary><div>${task.case_id ? `<span>案例版本</span><code>${esc(task.base_revision || '未提供')}</code><span>来源记录</span><code>${esc(task.base_event_id || '首次原始资料')}</code>` : `<span>产品</span><code>${esc(taskProduct(task))}</code>`}${context.materials?.length ? `<span>随任务固定的素材（${context.materials.length} 条）</span>${context.materials.map(item => `<p>${esc(item.title || item.text || item.source_id || item.kind || '素材')}</p>`).join('')}` : '<span>未附摘录篮条目</span>'}${context.linked_reviews?.length ? `<span>带入的关联评论（${context.linked_reviews.length} 条）</span>${context.linked_reviews.map(item => `<p>${esc(item.linked_as)} · ${esc(item.quote)}</p>`).join('')}` : ''}</div></details>`;
  }
  function roundMarkup(round,index) {
    return `<article class="ai-round" data-round-id="${esc(round.round_id || String(index))}"><div class="ai-round-heading"><button type="button" class="ai-round-toggle" aria-expanded="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg><h2>第 ${index+1} 轮</h2><span class="ai-round-summary">${esc(round.instruction || '')}</span></button><span class="ai-round-status ai-status ${esc(round.status)}">${esc(statusText(round))}</span><small class="ai-round-time">${esc(readableDate(round.completed_at || round.created_at))}</small></div>
      <div class="ai-round-body"><details class="ai-user-request" ${String(round.instruction || '').length <= 180 ? 'open' : ''}><summary>本轮要求 · ${esc(String(round.instruction || '').replace(/\s+/g,' ').slice(0,90))}${String(round.instruction || '').length > 90 ? '…' : ''}</summary><p>${esc(round.instruction || '')}</p></details><div class="ai-round-execution"></div><div class="task-activity-logs"></div>
      <div class="ai-output-heading"><h3 class="ai-output-title">实时回复</h3><div class="ai-output-actions"><button type="button" class="subtle ai-copy-round" hidden>${icon('copy')}复制当前文本</button></div></div><div class="ai-round-messages"></div><div class="ai-round-output markdown-body"></div><p class="ai-round-error ai-error" ${round.error ? '' : 'hidden'}>${esc(typeof round.error === 'string' ? round.error : round.error ? JSON.stringify(round.error) : '')}</p></div></article>`;
  }
  function renderTaskShell(task,keepScroll) {
    ai.page = task.task_id;
    $('#breadcrumb').innerHTML = '内容创作 <span>/</span> 任务详情';
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || pageURL('#ai'))}">${icon('back')}返回协作记录</a><div class="page-heading ai-task-heading"><div><p class="ai-task-meta"><span class="ai-status" id="ai-task-status"></span><span>${esc(actions[task.action] || 'AI 协作')}</span></p><h1>${esc(contextTitle(task))}</h1></div><button type="button" class="secondary compact" id="ai-cancel-task">取消处理</button></div>
      <div class="ai-task-context">${taskContextMarkup(task)}${experienceContextMarkup(task)}<details class="ai-version-details"><summary>任务编号</summary><code>${esc(task.task_id)}</code><button type="button" class="subtle compact" data-copy-task-id>复制编号</button></details></div><p id="ai-stale-warning" class="ai-stale-warning" hidden>案例已经更新。这个任务继续使用开始时的案例版本；若要基于新版讨论，请回到案例重新发起任务。</p>
      <details class="ai-task-management-fold"><summary>任务处置 · <span id="ai-management-summary">${esc(managementLabel(task))}</span></summary><section class="ai-task-management" aria-label="协作任务管理"><label for="ai-management-state">管理状态</label><select id="ai-management-state">${Object.entries(managementLabels(task)).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select><button type="button" class="secondary compact" id="ai-save-management">保存状态</button><button type="button" class="subtle" id="ai-task-archive">归档任务</button><span id="ai-management-note"></span></section></details>
      <div id="ai-task-adopted"></div>${task.action === 'retrospective' ? '<div id="ai-retro-card" class="feedback-chain ai-retro-card" hidden></div>' : ''}
      <div class="ai-task-progress"><p id="ai-task-progress" role="status"></p><small id="ai-stream-status" class="ai-stream-status"></small></div><p class="ai-error" id="ai-task-error" role="alert" hidden></p><div id="ai-rounds"></div>
      <section class="ai-continue-card ai-composer" aria-label="继续讨论"><form id="ai-continue-form"><label class="sr-only" for="ai-continue-instruction">下一步要求</label><textarea id="ai-continue-instruction" maxlength="12000" rows="2" required placeholder="${isWriting(task) ? '继续一起修改：例如保留第二版的推进结构，把正文压缩到 30 秒。' : task.action === 'retrospective' ? '继续复盘：补充观察、核对结论或讨论下一轮。' : '继续审稿：补充证据、核对意见或提出修改要求。'}"></textarea>
        ${defaultsMarkup('ai-continue')}<details class="ai-advanced" id="ai-continue-advanced"><summary><span>技能与服务</span><span class="ai-advanced-summary" id="ai-continue-settings-summary"></span></summary>${serviceMarkup()}<div id="ai-continue-settings"></div></details>
        <div class="ai-continue-footer"><p class="ai-muted" id="ai-continue-note"></p><small class="ai-shortcut-hint">Ctrl + Enter 提交</small><button type="submit" class="primary compact" id="ai-continue-submit">继续修改</button></div></form></section>
      <nav id="ai-variant-nav" class="ai-variant-nav" aria-label="变体导航" hidden><button type="button" class="ai-variant-top" data-variant-top aria-label="回到顶部"><span class="ai-variant-tip"><b>回到顶部</b></span></button><div class="ai-variant-list"></div></nav>`;
    window.WorkbenchLinks?.mount({kind:'task',record:task});
    bindVariantNav();
    $('#ai-continue-instruction').value = ai.drafts.get(task.task_id) || ai.pending.get(`continue:${task.task_id}`)?.payload.instruction || '';
    renderSettings('ai-continue',settingsForTask(task));
    $('#ai-continue-instruction').addEventListener('input',event => {ai.drafts.set(task.task_id,event.target.value);updateTaskControls();});
    $('#ai-continue-form').addEventListener('submit',submitContinue);$('#ai-cancel-task').onclick = cancelTask;connectServiceButton();
    $('[data-copy-task-id]').onclick = () => copyText(task.task_id,'任务编号已复制');
    $('#ai-management-state').value = managementState(task);
    $('#ai-management-state').dataset.persistedState = managementState(task);
    $('#ai-save-management').onclick = () => manageTasks(`state:${ai.task.task_id}`,[ai.task],{management_state:$('#ai-management-state').value});
    $('#ai-task-archive').onclick = () => manageTasks(`archive:${ai.task.task_id}`,[ai.task],{archived:!ai.task.archived});
    // Older rounds fold to one line; a click opens any of them.
    $('#ai-rounds').addEventListener('click',event => {
      const toggle = event.target.closest('.ai-round-toggle');
      if (toggle) {const node = toggle.closest('.ai-round'), key = `${task.task_id}:${node.dataset.roundId}`;(ai.openRounds ||= new Map()).set(key,node.classList.contains('is-collapsed'));paintRoundFolding();updateVariantNav();return;}
      const menu = event.target.closest('.ai-menu');if (menu && event.target.closest('[role=menuitem]')) menu.open = false;
    });
    if (!keepScroll) window.scrollTo(0,0);
  }
  function paintRoundFolding() {
    const nodes = $$('#ai-rounds > .ai-round');
    nodes.forEach((node,index) => {
      const chosen = ai.openRounds?.get(`${ai.task?.task_id}:${node.dataset.roundId}`), open = chosen ?? index === nodes.length - 1;
      node.classList.toggle('is-collapsed',!open);$('.ai-round-toggle',node).setAttribute('aria-expanded',String(open));
    });
  }
  // Right-edge outline of every "变体 X" heading in the open rounds: fixed on screen, click to jump, one tap back to top.
  function variantHeadings() {
    const open = $$('#ai-rounds > .ai-round:not(.is-collapsed)'), found = [];
    open.forEach((round,roundIndex) => {
      $$('.markdown-body h2,.markdown-body h3,.markdown-body h4,.markdown-body h5,.markdown-body p',round).forEach(heading => {
        if (heading.closest('[hidden]')) return;
        // Variants are usually a bold-only line ("**202H-S2 · 变体 #1 · …**"), not a markdown heading.
        if (heading.tagName === 'P' && !(heading.children.length === 1 && heading.firstElementChild.tagName === 'STRONG' && (heading.textContent || '').trim() === (heading.firstElementChild.textContent || '').trim())) return;
        const match = /^.{0,40}?变体\s*#?\s*([A-Za-z0-9]+|[一二三四五六七八九十]+)/.exec(heading.textContent || '');
        if (!match) return;
        const id = match[1].toUpperCase(), label = `变体${id}`;
        let preview = '', node = heading.nextElementSibling;
        while (node && !preview && !/^H[2-5]$/.test(node.tagName)) {preview = (node.textContent || '').trim().replace(/\s+/g,' ');node = node.nextElementSibling;}
        found.push({heading,label:open.length > 1 ? `第 ${$$('#ai-rounds > .ai-round').indexOf(round)+1} 轮 · ${label}` : label,title:(heading.textContent || '').trim(),preview:preview.slice(0,90)});
      });
    });
    return found;
  }
  function updateVariantNav() {
    const nav = $('#ai-variant-nav');if (!nav) return;
    const items = variantHeadings(), list = $('.ai-variant-list',nav);
    ai.variantItems = items;
    nav.hidden = !items.length;
    const signature = items.map(item => `${item.label}${item.title}${item.preview}`).join('|');
    if (list.dataset.source !== signature) {
      list.dataset.source = signature;
      list.innerHTML = items.map((item,index) => `<button type="button" class="ai-variant-item" data-variant-index="${index}" aria-label="${esc(item.label)}"><span class="ai-variant-tip"><b>${esc(item.label)}</b>${item.title && item.title !== item.label ? `<span>${esc(item.title)}</span>` : ''}${item.preview ? `<em>${esc(item.preview)}</em>` : ''}</span></button>`).join('');
    }
    markVariantNav();
  }
  function markVariantNav() {
    const nav = $('#ai-variant-nav');if (!nav) return; // the scroll listener outlives the task page
    const items = ai.variantItems || [], buttons = $$('.ai-variant-item',nav);
    let current = -1;
    items.forEach((item,index) => {if (item.heading.isConnected && item.heading.getBoundingClientRect().top <= 120) current = index;});
    buttons.forEach((button,index) => button.classList.toggle('is-current',index === current));
  }
  function bindVariantNav() {
    const nav = $('#ai-variant-nav');if (!nav) return;
    nav.addEventListener('click',event => {
      if (event.target.closest('[data-variant-top]')) {window.scrollTo({top:0,behavior:'smooth'});return;}
      const button = event.target.closest('[data-variant-index]'), item = button && ai.variantItems?.[Number(button.dataset.variantIndex)];
      if (item?.heading.isConnected) item.heading.scrollIntoView({block:'start',behavior:'smooth'});
    });
    if (!ai.variantScrollBound) {
      ai.variantScrollBound = true;
      let frame = 0;
      window.addEventListener('scroll',() => {if (frame) return;frame = requestAnimationFrame(() => {frame = 0;markVariantNav();});},{passive:true});
    }
  }
  function focusRequestedRound() {
    const hash = pageHash(), params = new URLSearchParams(hash.split('?').slice(1).join('?')), round = params.get('round');
    if (!round || ai.focusedRound === hash) return;
    const node = $$('#ai-rounds > .ai-round').find(item => item.dataset.roundId === round);
    if (!node) return;
    ai.focusedRound = hash;
    (ai.openRounds ||= new Map()).set(`${ai.task.task_id}:${round}`,true);
    paintRoundFolding();
    if (params.get('adopted')) return;
    node.tabIndex = -1;node.focus({preventScroll:true});node.scrollIntoView({block:'start'});
  }
  // Polls repaint status, notices and rounds in place; whatever changes above the reader must not move the text.
  function updateTask(task,keepScroll = true) {keepReadingPlace(() => paintTask(task,keepScroll));}
  function paintTask(task,keepScroll) {
    ai.task = task;
    rememberTaskState(task);
    if (ai.page !== task.task_id || !$('#ai-rounds')) renderTaskShell(task,keepScroll);
    // Real adopted records remain reachable even when they came from a legacy non-writing task.
    void window.CaseScripts?.mountTaskAdopted?.($('#ai-task-adopted'),task);
    // Read the chart again only when the task itself changed, not on every poll.
    const retro = $('#ai-retro-card'), retroKey = JSON.stringify([task.task_id,task.status,task.updated_at,(task.rounds || []).length]);
    if (retro && retro.dataset.taskKey !== retroKey) {retro.dataset.taskKey = retroKey;void window.FeedbackChain?.mountTaskCard?.(retro,task);}
    const badge = $('#ai-task-status');badge.className = `ai-status ${task.status}`;setText('#ai-task-status',statusText(task));
    const managementSelect = $('#ai-management-state');
    if (managementSelect && managementSelect.dataset.persistedState !== managementState(task)) {managementSelect.value = managementState(task);managementSelect.dataset.persistedState = managementState(task);}
    $('#ai-stale-warning').hidden = !task.stale;
    const progress = typeof task.progress === 'string' ? task.progress : task.progress?.message || '';
    setText('#ai-task-progress',(task.stopping || ai.cancelWaiting.has(task.task_id)) && active(task) ? '已请求取消，正在等待处理进程停止…' : active(task) ? progress || '任务已进入处理队列，结果会自动更新。' : '');
    $('.ai-task-progress').hidden = !active(task);
    setText('#ai-management-summary',managementLabel(task) + (task.archived ? ' · 已归档' : ''));
    const container = $('#ai-rounds'), rounds = task.rounds || [];
    rounds.forEach((round,index) => {
      const key = String(round.round_id || index);let node = [...container.children].find(child => child.dataset.roundId === key);
      if (!node) {const holder = document.createElement('div');holder.innerHTML = roundMarkup(round,index);node = holder.firstElementChild;container.append(node);window.TaskActivity?.mountLogs($('.task-activity-logs',node),{owner_id:task.task_id,source_id:round.round_id,quiet:true});}
      const status = $('.ai-round-status',node);status.className = `ai-round-status ai-status ${round.status}`;if (status.textContent !== statusText(round)) status.textContent = statusText(round);
      const execution = $('.ai-round-execution',node), metadata = roundMetadata(round);
      if (execution.dataset.source !== metadata && !selectionInside(execution)) {execution.innerHTML = metadata;execution.dataset.source = metadata;}
      updateRoundMessages(node,round);
      const error = $('.ai-round-error',node), reason = typeof round.error === 'string' ? round.error : round.error ? JSON.stringify(round.error) : '';
      error.hidden = !reason;if (error.textContent !== reason) error.textContent = reason;
      const time = $('.ai-round-time',node), value = readableDate(round.completed_at || round.created_at);if (time.textContent !== value) time.textContent = value;
    });
    paintRoundFolding();
    focusRequestedRound();
    updateVariantNav();
    if (!active(task)) {ai.cancelWaiting.delete(task.task_id);ai.pending.delete(`cancel:${task.task_id}`);}
    updateTaskControls();syncStream(task);
  }
  function updateTaskControls() {keepReadingPlace(paintTaskControls);}
  function paintTaskControls() {
    const task = ai.task;if (!task || !$('#ai-continue-form')) return;
    const managementBusy = !!ai.managing || active(task);
    $('#ai-management-state').disabled = managementBusy || ai.pending.has(`manage:state:${task.task_id}`);
    $('#ai-save-management').disabled = managementBusy;
    $('#ai-task-archive').disabled = managementBusy;
    setText('#ai-save-management',ai.pending.has(`manage:state:${task.task_id}`) ? '重试保存' : '保存状态');
    setText('#ai-task-archive',ai.pending.has(`manage:archive:${task.task_id}`) ? '重试整理' : task.archived ? '恢复任务' : '归档任务');
    setText('#ai-management-note',task.archived ? '已归档 · 各轮回答与来源关联均保留。继续讨论会恢复为活跃任务。' : active(task) ? '本轮结束后可保存管理状态或归档。' : isWriting(task) ? '管理状态由你确认；归档后可在历史中恢复。' : '仅整理此任务；正式复盘决定或案例修改请回到来源记录。');
    const key = `continue:${task.task_id}`, pending = ai.pending.get(key), cancel = ai.pending.get(`cancel:${task.task_id}`), waiting = task.stopping || ai.cancelWaiting.has(task.task_id), busy = pending?.busy || cancel?.busy || ai.managing;
    const input = $('#ai-continue-instruction');input.readOnly = !!pending;
    $('#ai-continue-submit').disabled = !!busy || (active(task) && !pending) || !ai.status?.available || !input.value.trim() || (!pending && !!settingsIssue(settingsForTask(task)));
    if ($('#ai-continue-fieldset')) $('#ai-continue-fieldset').disabled = !!pending;
    const continuation = isWriting(task) ? '继续修改' : task.action === 'retrospective' ? '继续复盘' : '继续审稿';
    setText('#ai-continue-submit',pending?.busy ? '正在提交…' : pending ? '重试同一请求' : ['failed','interrupted','cancelled'].includes(task.status) ? continuation + '并重试' : continuation);
    const button = $('#ai-cancel-task');button.hidden = !active(task);button.disabled = !!busy || waiting;
    button.textContent = cancel?.busy ? '正在请求取消…' : waiting ? '等待取消完成…' : cancel ? '重试取消请求' : '取消处理';
    setText('#ai-continue-note',pending ? '上次提交尚待核对；重试会使用相同要求，避免重复增加一轮。' : active(task) ? '本轮处理期间可以先写下一步要求，完成后再提交。' : ['failed','interrupted','cancelled'].includes(task.status) ? '任务已停止。填写要求并提交，会明确开启新一轮尝试。' : '继续使用任务开始时固定的资料，并保留此前每轮记录。');
    setText('#ai-service-label',availabilityText());
    setText('#ai-continue-defaults-summary',defaultsSummary());
    const issue = !pending && ai.options && !ai.optionsLoading && settingsIssue(settingsForTask(task));
    if (issue && $('#ai-continue-advanced')) $('#ai-continue-advanced').open = true;
    setText('#ai-continue-settings-summary',issue ? '需要处理' : ai.status?.available === false ? 'AI 暂不可用' : '');
    $('#ai-continue-settings-summary')?.classList.toggle('is-warn',!!issue || ai.status?.available === false);
    const error = ai.errors.get(task.task_id) || '';setText('#ai-task-error',error);$('#ai-task-error').hidden = !error;
  }
  async function submitContinue(event) {
    event.preventDefault();const task = ai.task;if (!task || (active(task) && !ai.pending.has(`continue:${task.task_id}`)) || !ai.status?.available) return;
    const instruction = $('#ai-continue-instruction').value.trim(), key = `continue:${task.task_id}`;
    if (!instruction || ai.pending.get(key)?.busy || (!ai.pending.has(key) && settingsIssue(settingsForTask(task)))) return;
    let pending = ai.pending.get(key);
    if (!pending) {pending = {path:`/api/ai/tasks/${encodeURIComponent(task.task_id)}/continue`,payload:{request_id:crypto.randomUUID(),task_revision:task.revision,instruction,skill_selection:settingsForTask(task).skill_selection}};ai.pending.set(key,pending);}
    pending.busy = true;closeStream();ai.mutation++;ai.errors.delete(task.task_id);updateTaskControls();
    try {
      const response = await api(pending.path,{method:'POST',body:pending.payload});
      if (response.task?.task_id !== task.task_id) throw new Error('没有收到完整任务信息，请重试同一请求核对。');
      ai.pending.delete(key);ai.drafts.delete(task.task_id);
      if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) {$('#ai-continue-instruction').value = '';updateTask(response.task);}
      else rememberTaskState(response.task);
      toast('新一轮要求已提交。');
    } catch (error) {
      if (definitive(error)) ai.pending.delete(key);
      ai.errors.set(task.task_id,error.status === 409 ? '任务已更新，本次未追加要求。正在读取最新记录，请核对后再提交。' : definitive(error) ? message(error) : `提交结果尚未确认：${message(error)}。请重试同一请求核对。`);
      if (error.status === 404) {if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) renderMissing();return;}
      if (error.status === 409) void pollTask(true);
    } finally {pending.busy = false;updateTaskControls();syncStream(ai.task);void loadTaskList(0,true).catch(() => {});}
  }
  // A format-only round re-lays out an earlier candidate; the server keeps it only if every spoken word matches.
  async function formatRound(task,round) {
    const key = `format:${task.task_id}:${round.round_id}`;let pending = ai.pending.get(key);
    if (pending?.busy) return false;
    if (!pending) {pending = {path:`/api/ai/tasks/${encodeURIComponent(task.task_id)}/continue`,payload:{request_id:crypto.randomUUID(),task_revision:(ai.task?.task_id === task.task_id ? ai.task : task).revision,format_round_id:round.round_id}};ai.pending.set(key,pending);}
    pending.busy = true;closeStream();ai.mutation++;ai.errors.delete(task.task_id);
    try {
      const response = await api(pending.path,{method:'POST',body:pending.payload});
      ai.pending.delete(key);
      if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) updateTask(response.task);else rememberTaskState(response.task);
      toast('已开始整理格式（只改排版，逐字核对）。完成后在新一轮里点「制作视频」。');
      return true;
    } catch (error) {
      if (definitive(error)) ai.pending.delete(key);
      throw error;
    } finally {pending.busy = false;updateTaskControls();syncStream(ai.task);void loadTaskList(0,true).catch(() => {});}
  }
  async function cancelTask() {
    const task = ai.task;if (!task || !active(task) || task.stopping || ai.cancelWaiting.has(task.task_id)) return;
    const key = `cancel:${task.task_id}`;let pending = ai.pending.get(key);
    if (pending?.busy) return;
    if (!pending) {
      const roundId = task.rounds?.at(-1)?.round_id;
      if (!roundId) {ai.errors.set(task.task_id,'当前轮次信息不完整，请重新读取任务后再取消。');updateTaskControls();void pollTask(true);return;}
      pending = {path:`/api/ai/tasks/${encodeURIComponent(task.task_id)}/cancel`,payload:{request_id:crypto.randomUUID(),round_id:roundId}};ai.pending.set(key,pending);
    }
    pending.busy = true;closeStream();ai.mutation++;ai.errors.delete(task.task_id);updateTaskControls();
    try {
      const response = await api(pending.path,{method:'POST',body:pending.payload});
      if (response.task?.task_id !== task.task_id) throw new Error('没有收到完整取消结果，请重试同一请求核对。');
      ai.pending.delete(key);
      if (active(response.task)) ai.cancelWaiting.add(task.task_id);
      if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) updateTask(response.task);
      else rememberTaskState(response.task);
    } catch (error) {
      if (definitive(error)) ai.pending.delete(key);
      ai.errors.set(task.task_id,definitive(error) ? message(error) : `取消结果尚未确认：${message(error)}。可重试同一取消请求。`);
      if (error.status === 404) {if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) renderMissing();return;}
    } finally {pending.busy = false;updateTaskControls();syncStream(ai.task);void loadTaskList(0,true).catch(() => {});}
  }
  function renderMissing() {
    closeStream();
    const id = ai.task?.task_id || taskRouteId();ai.task = null;ai.page = 'missing';if (id) {ai.errors.delete(id);ai.cancelWaiting.delete(id);ai.drafts.delete(id);ai.pending.delete(`continue:${id}`);ai.pending.delete(`cancel:${id}`);}
    $('#content').innerHTML = '<div class="empty"><h3>任务或关联案例已不存在</h3><p>旧任务内容已从当前页面清空，请返回协作记录查看现有记录。</p><a class="secondary" href="#ai">返回协作记录</a></div>';
  }
  function closeStream() {
    if (ai.stream) ai.stream.source.close();
    ai.stream = null;
    if (ai.streamFrame !== null) clearTimeout(ai.streamFrame);
    ai.streamFrame = null;ai.queuedTask = null;
  }
  function streamCurrent(session) {return ai.stream === session && pagePath() === `#ai/${encodeURIComponent(session.taskId)}` && session.mutation === ai.mutation;}
  function olderSnapshot(task,previous = ai.task) {
    if (!previous || task.task_id !== previous.task_id) return false;
    const oldTime = Date.parse(previous.updated_at || ''), newTime = Date.parse(task.updated_at || '');
    if (Number.isFinite(oldTime) && Number.isFinite(newTime) && newTime < oldTime) return true;
    return (task.rounds?.length || 0) < (previous.rounds?.length || 0);
  }
  function streamFallback(session) {
    if (!streamCurrent(session)) return;
    closeStream();ai.streamRetryAt = Date.now()+10000;
    setText('#ai-stream-status','实时连接中断，已改为定时更新。');void pollTask(true);
  }
  function queueStreamTask(session,task) {
    if (!streamCurrent(session) || task?.task_id !== session.taskId) return;
    if (olderSnapshot(task,ai.queuedTask || ai.task)) return;
    session.lastEvent = Date.now();ai.queuedTask = task;
    if (ai.streamFrame !== null) return;
    ai.streamFrame = setTimeout(() => {
      ai.streamFrame = null;const next = ai.queuedTask;ai.queuedTask = null;
      if (next && streamCurrent(session)) updateTask(next,true);
    },100);
  }
  function syncStream(task) {
    if (!task || !active(task) || document.hidden || pagePath() !== `#ai/${encodeURIComponent(task.task_id)}` || [...ai.pending.values()].some(entry => entry.busy)) {
      closeStream();if (task && !active(task)) setText('#ai-stream-status','本轮已结束。');return;
    }
    if (ai.stream?.taskId === task.task_id) {
      setText('#ai-stream-status',ai.stream.connected ? '实时连接 · 回复持续更新' : '正在连接实时回复…');return;
    }
    closeStream();
    if (typeof EventSource === 'undefined' || Date.now() < ai.streamRetryAt) {setText('#ai-stream-status','正在定时更新回复。');return;}
    try {
      const source = new EventSource(`/api/ai/tasks/${encodeURIComponent(task.task_id)}/stream`);
      const session = {source,taskId:task.task_id,mutation:ai.mutation,connected:false,lastEvent:Date.now()};ai.stream = session;
      setText('#ai-stream-status','正在连接实时回复…');
      source.onopen = () => {if (streamCurrent(session)) {session.connected = true;session.lastEvent = Date.now();setText('#ai-stream-status','实时连接 · 回复持续更新');}};
      source.onmessage = event => {if (!streamCurrent(session)) return;try {const data = JSON.parse(event.data);if (!data.task || data.task.task_id !== task.task_id) throw new Error('Invalid task snapshot');queueStreamTask(session,data.task);} catch {streamFallback(session);}};
      source.onerror = () => streamFallback(session);
    } catch {ai.streamRetryAt = Date.now()+10000;setText('#ai-stream-status','实时连接暂不可用，正在定时更新回复。');}
  }
  async function pollTask(force = false) {
    const id = taskRouteId();
    if (!id || ai.page === 'missing' || ai.polling || (!force && [...ai.pending.values()].some(entry => entry.busy))) return;
    if (!force && ai.stream?.taskId === id && Date.now()-ai.stream.lastEvent < 15000) return;
    ai.polling = true;const mutation = ai.mutation, sequence = ai.sequence;
    try {
      const data = await api(`/api/ai/tasks/${encodeURIComponent(id)}`), task = data.task || data;
      if (sequence !== ai.sequence || mutation !== ai.mutation || pagePath() !== `#ai/${encodeURIComponent(id)}`) return;
      if (task.task_id !== id) throw new Error('任务详情不完整。');
      if (olderSnapshot(task)) return;
      if (ai.errors.get(id)?.startsWith('暂时无法更新任务')) ai.errors.delete(id);
      updateTask(task,true);
    } catch (error) {
      if (sequence !== ai.sequence || mutation !== ai.mutation || pagePath() !== `#ai/${encodeURIComponent(id)}`) return;
      if (error.status === 404) renderMissing();
      else {ai.errors.set(id,`暂时无法更新任务：${message(error)}。已有内容和下一步要求仍保留。`);updateTaskControls();}
    } finally {ai.polling = false;}
  }
  async function routeAI(keepScroll = false) {
    const hash = pageHash(), sequence = ++ai.sequence;
    ai.focusedRound = '';
    if (pagePath() === '#ai') {
      const product = window.WorkbenchShell?.product() || '';
      if (ai.page === 'list' && $('#ai-task-list')) {
        const saved = window.WorkbenchShell?.listState?.('ai',product);
        if (saved || ai.listProduct !== product) {restoreListState(saved);ai.listProduct = product;}
      }
      closeStream();await renderList(keepScroll,sequence);return;
    }
    if (!hash.startsWith('#ai/')) return;
    const id = taskRouteId();
    if (!id) {renderMissing();return;}
    if (ai.task?.task_id === id && alignTaskProduct(ai.task)) return;
    if (ai.stream && ai.stream.taskId !== id) closeStream();
    if (ai.page === id && $('#ai-rounds')) {
      focusRequestedRound();
      void window.CaseScripts?.mountTaskAdopted?.($('#ai-task-adopted'),ai.task);
      await pollTask();return;
    }
    ai.page = '';ai.task = null;$('#content').innerHTML = skeletonMarkup(2);
    try {
      void Promise.all([checkStatus(),loadOptions(false,true)]).then(() => {
        if (sequence !== ai.sequence || pageHash() !== hash || ai.task?.task_id !== id) return;
        setText('#ai-service-label',availabilityText());
        renderSettings('ai-continue',settingsForTask(ai.task));
        updateTaskControls();
      });
      const data = await api(`/api/ai/tasks/${encodeURIComponent(id)}`);
      if (sequence !== ai.sequence || pageHash() !== hash) return;
      const task = data.task || data;if (task.task_id !== id) throw new Error('任务详情不完整。');
      if (alignTaskProduct(task)) return;
      updateTask(task,keepScroll);
    } catch (error) {if (sequence !== ai.sequence || pageHash() !== hash) return;if (error.status === 404) renderMissing();else $('#content').innerHTML = `<div class="empty"><h3>暂时无法读取任务</h3><p>${esc(message(error))}</p><button type="button" class="secondary" id="retry-ai-detail">重新读取</button></div>`;$('#retry-ai-detail')?.addEventListener('click',() => routeAI(true));}
  }
  // Running and completed work share the same content entry; the badge still shows active AI tasks.
  $('#ai-nav').onclick = () => {location.hash = pageURL(window.WorkbenchGuest ? '#ai' : '#content');};
  $('#ai-create-form').addEventListener('submit',submitCreate);
  $('#ai-create-instruction').addEventListener('input',event => {if (ai.modal) {ai.modal.instruction = event.target.value;updateCreateControls();}});
  // Ctrl/Cmd+Enter submits from either writing box, through the form's own checks.
  document.addEventListener('keydown',event => {
    if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
    const box = event.target.closest?.('#ai-create-instruction,#ai-continue-instruction');if (!box) return;
    event.preventDefault();const submit = box.id === 'ai-create-instruction' ? $('#submit-ai-create') : $('#ai-continue-submit');
    if (submit && !submit.disabled) box.form.requestSubmit(submit);
  });
  $('#close-ai-create').onclick = $('#cancel-ai-create').onclick = closeCreate;
  $('#ai-create-dialog').addEventListener('cancel',event => {event.preventDefault();closeCreate();});
  $('#ai-create-check').addEventListener('click',async () => {await Promise.all([checkStatus(true),loadOptions(true)]);if (ai.modal) renderSettings('ai-create',ai.modal.settings);updateCreateControls();});
  $('#ai-delete-form').addEventListener('submit',submitDelete);
  $('#close-ai-delete').onclick = $('#cancel-ai-delete').onclick = closeDeleteDialog;
  $('#ai-delete-dialog').addEventListener('cancel',event => {event.preventDefault();closeDeleteDialog();});
  window.addEventListener('hashchange',() => {if (ai.stream && pagePath() !== `#ai/${encodeURIComponent(ai.stream.taskId)}`) closeStream();});
  document.addEventListener('visibilitychange',() => {if (document.hidden) closeStream();else {void loadTaskList().catch(() => {});if (ai.modal || pageHash().startsWith('#ai/')) void loadOptions(true).then(refreshSettingsViews);if (pageHash().startsWith('#ai/')) {syncStream(ai.task);void pollTask(true);}}});
  setInterval(() => {
    if (document.hidden) return;
    if (ai.modal) updateCreateControls();
    if (pageHash().startsWith('#ai/')) void pollTask();
    else if (pageHash() === '#ai' && !ai.loading) {ai.loading = true;renderList(true,ai.sequence).finally(() => ai.loading = false);}
    // Off the AI page the list only feeds the nav count: refresh quickly while a task runs, slowly when idle.
    if (pageHash() !== '#ai') void loadTaskList((ai.taskList?.tasks || []).some(active) ? 5000 : 30000).catch(() => {});
  },2500);
  void loadTaskList().catch(() => {});
  if (state.detail && pageHash() === `#case/${state.detail.case_id}` && !$('.ai-case-actions')) void mountCaseActions(state.detail);
  const closeMenus = event => $$('.ai-menu[open]').forEach(menu => {if (event.key === 'Escape' || !menu.contains(event.target)) menu.open = false;});
  window.addEventListener('click',closeMenus,true);window.addEventListener('keydown',event => {if (event.key === 'Escape') closeMenus(event);});
  return {mountCaseActions,startWriting,startFromDirection,startBlank,startWithMaterials,modalOpen,hasUnsavedInput,refreshAvailability,refreshDefaults,captureListState,route:routeAI,formatRound,taskDisplayTitle:contextTitle,taskManagementLabel:managementLabel};
})();
