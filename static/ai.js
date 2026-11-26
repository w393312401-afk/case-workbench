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
  const continuationHint = task => isWriting(task) ? '继续一起修改：例如保留第二版的推进结构，把正文压缩到 30 秒。' : task.action === 'retrospective' ? '继续复盘：补充观察、核对结论或讨论下一轮。' : '继续审稿：补充证据、核对意见或提出修改要求。';
  function planPayload(modal) {
    if (!planActive(modal)) return {};
    const direction = planDirection(modal);
    return {variants:[...modal.variantSel].sort((a,b) => a - b).map(index => ({index,progression:direction.variants[index].progression ?? null})),new_variant_count:modal.newVariants};
  }
  const statuses = {queued:'排队中',running:'正在处理',completed:'已完成',failed:'执行失败',cancelled:'已取消',interrupted:'执行中断'};
  const managementStates = {pending_selection:'待选稿',needs_revision:'继续修改',adopted:'已采纳',abandoned:'已放弃',finished:'已完结'};
  const managementState = task => task.management_state || 'pending_selection';
  const managementFilterLabels = {pending_selection:'待选稿 / 确认结果',needs_revision:'继续修改 / 讨论',adopted:'已采纳 / 确认结果',abandoned:'已放弃',finished:'已完结'};
  const managementLabels = task => isWriting(task) ? managementStates : {pending_selection:'待确认结果',needs_revision:'继续讨论',adopted:'已确认结果',abandoned:'已放弃'};
  const managementLabel = task => managementLabels(task)[managementState(task)] || '待查看';
  const taskStarted = task => task.task_started_label || (task.created_at ? `${readableDate(task.created_at)} 发起` : '');
  const listViews = {active:'活跃',history:'历史',all:'全部记录'};
  const active = task => !!task?.busy || !!task?.stopping || ['queued','running'].includes(task?.status);
  const retryable = task => !active(task) && (!Array.isArray(task?.rounds) || task.rounds.length > 0) && ['failed','interrupted','cancelled'].includes(task?.status);
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
  function paintNavCount(count) {
    if (ai.navCount === count) return;
    ai.navCount = count;
    const badge = $('#ai-active-count');
    if (!badge) return;
    badge.textContent = String(count);
    badge.hidden = count === 0;
    badge.setAttribute('aria-label',`${count} 项运行中任务`);badge.title = `${count} 个 AI 任务运行中，在内容列表查看进度`;
    document.querySelectorAll('[data-ai-running]').forEach(node => {node.textContent = count ? String(count) : '';node.hidden = count === 0;});
  }
  function updateNavCount(tasks) {
    const byProduct = Object.create(null);
    tasks.filter(active).forEach(task => {const product = taskProduct(task);byProduct[product] = (byProduct[product] || 0) + 1;});
    ai.taskCounts = {active:Object.values(byProduct).reduce((sum,count) => sum + count,0),by_product:byProduct};
    ai.taskCountsAt = Date.now();ai.taskCountsVersion = (ai.taskCountsVersion || 0) + 1;
    paintNavCount(scopedTasks(tasks).filter(active).length);
  }
  function paintTaskCounts(counts) {
    const shell = window.WorkbenchShell;
    paintNavCount(!shell?.product() ? counts.active : Object.entries(counts.by_product).reduce((sum,[product,count]) => sum + (shell.matchesProduct(product) ? count : 0),0));
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
    ai.taskListRequest = api('/api/ai/tasks').then(receiveTaskList).finally(() => {
      ai.taskListRequest = null;
      if (ai.taskListRefreshAfter) {ai.taskListRefreshAfter = false;void loadTaskList(0,true).catch(() => {});}
    });
    return ai.taskListRequest;
  }
  function receiveTaskList(data) {
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
  }
  function loadNavCounts(maxAge = 5000) {
    if (ai.countsSupported === false) return loadTaskList(maxAge);
    if (ai.taskCounts && Date.now() - ai.taskCountsAt < maxAge) {paintTaskCounts(ai.taskCounts);return Promise.resolve(ai.taskCounts);}
    if (ai.taskCountsRequest) return ai.taskCountsRequest;
    const version = ai.taskCountsVersion || 0;
    ai.taskCountsRequest = (async () => {
      let data;
      try {data = await api('/api/ai/tasks?projection=counts');}
      catch (error) {
        if (![400,404].includes(error.status)) throw error;
        ai.countsSupported = false;return loadTaskList(0,true);
      }
      // Older engines ignore the projection query and return ordinary cards.
      if (Array.isArray(data?.tasks)) {ai.countsSupported = false;return receiveTaskList(data);}
      const counts = data?.counts;
      if (!counts || !Number.isSafeInteger(counts.active) || counts.active < 0 || !counts.by_product ||
          typeof counts.by_product !== 'object' || Array.isArray(counts.by_product) ||
          Object.values(counts.by_product).some(count => !Number.isSafeInteger(count) || count < 0)) throw new Error('任务数量暂时无法读取。');
      if (version !== (ai.taskCountsVersion || 0)) return ai.taskCounts;
      ai.taskCounts = counts;ai.taskCountsAt = Date.now();paintTaskCounts(counts);return counts;
    })().finally(() => {ai.taskCountsRequest = null;});
    return ai.taskCountsRequest;
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
    if (ai.options && ai.options.default_source !== 'kind') {
      ai.options.default_settings = ai.savedDefaults;
      ai.options.defaults_configured = true;
      if (ai.options.defaults_error) {ai.options.defaults_error = '';ai.options.options_error = '';}
    }
    refreshSettingsViews();
    if ((ai.modal || ai.task) && (!ai.options || ai.options.options_error || (ai.savedDefaults.model && !selectedModel(ai.savedDefaults)))) void loadOptions(true).then(refreshSettingsViews);
  }
  function refreshSettingsViews() {
    if (ai.modal) {renderSettings('ai-create',ai.modal.settings);renderCreateModel(ai.modal);}
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
        const own = (Array.isArray(result.kind_defaults) ? result.kind_defaults : []).find(row => row?.kind === 'writing' && row.source === 'kind');
        const current = (ai.defaultsRevision || 0) === revision, defaults = own || (current ? result.default_settings : ai.savedDefaults);
        const defaultsError = current ? result.defaults_error || '' : '';
        ai.options = {models:Array.isArray(result.models) ? result.models : [],skills:Array.isArray(result.skills) ? result.skills : [],default_settings:{model:defaults?.model || null,reasoning_effort:defaults?.reasoning_effort || null},defaults_configured:current ? !!result.defaults_configured : true,defaults_error:defaultsError,options_error:defaultsError,models_error:result.models_error || '',default_source:own ? 'kind' : 'global'};
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
    if (settings.override) {
      const chosen = selectedModel(settings);
      if (!settings.model) return '请为本次写稿选择模型，或改回全局默认。';
      if (settings.model && !chosen) return '所选模型当前不可选，请重新选择或改回全局默认。';
      if (chosen?.available === false) return '所选模型目前不可用，请重新选择或改回全局默认。';
      if (settings.reasoning_effort && !allowedEfforts(settings).includes(settings.reasoning_effort)) return '所选模型不支持这一思考强度，请重新选择。';
      return skillAvailable(settings.skill_selection) ? '' : '所选技能目前不可用，请改为自动匹配或选择其他技能。';
    }
    const defaults = ai.options.default_settings || {}, selected = selectedModel(defaults);
    if (defaults.model && !selected) return '全局默认模型当前不可选，请在全局设置中更新模型。';
    if (selected?.available === false) return ai.status?.providers?.[providerOf(selected)]?.error || (providerOf(selected) === 'claude' ? '全局默认模型使用的 Claude Code 尚未就绪，请在全局设置中检查。' : '全局默认模型目前不可用，请重新检查或在全局设置中更新模型。');
    if (defaults.reasoning_effort && !allowedEfforts(defaults).includes(defaults.reasoning_effort)) return '全局默认模型不支持已保存的思考强度，请在全局设置中更新。';
    if (!skillAvailable(settings.skill_selection)) return '所选技能目前不可用，请改为自动匹配或选择其他技能。';
    return '';
  }
  function settingsMarkup(prefix,settings) {
    return `<fieldset class="ai-run-settings" id="${prefix}-fieldset"><p class="ai-settings-issue" role="status" ${settingsIssue(settings) ? '' : 'hidden'}>${esc(settingsIssue(settings))}</p></fieldset>`;
  }
  const providerOf = model => model?.provider || (String(model?.id || '').startsWith('antigravity:') ? 'antigravity' : String(model?.id || '').startsWith('claude-') ? 'claude' : 'codex');
  const ownDefault = () => ai.options?.default_source === 'kind';
  function defaultsSummary() {
    if (!ai.options || ai.optionsLoading) return '正在读取全局默认配置…';
    if (ai.options.options_error) return '全局默认配置暂不可用';
    const defaults = ai.options.default_settings || {}, model = selectedModel(defaults);
    const label = model?.name || (defaults.model ? modelLabel(defaults.model) : '本机默认模型');
    const effort = defaults.reasoning_effort ? `思考强度：${effortNames[defaults.reasoning_effort] || defaults.reasoning_effort}` : '默认思考强度';
    return `${ownDefault() ? '沿用上次写稿的选择' : '使用全局默认'} · ${label} · ${effort}`;
  }
  function defaultsMarkup(prefix) {
    return `<p class="ai-model-description ai-global-defaults"><span id="${prefix}-defaults-summary">${esc(defaultsSummary())}</span> <a href="${esc(pageURL('#settings/ai'))}" target="_blank" rel="noopener" title="在新标签页打开，保留当前输入">全局设置</a></p>`;
  }
  // The writing dialog may pick a model for this one task; leaving it on 「全局默认」 keeps the shared setting.
  function createModelMarkup(settings) {
    if (!ai.options || ai.optionsLoading || ai.options.options_error) return defaultsMarkup('ai-create');
    const provider = settings.override ? (settings.provider || providerOf(selectedModel(settings) || {id:settings.model})) : '__default__';
    const models = modelChoices().filter(model => providerOf(model) === provider);
    const efforts = settings.override ? allowedEfforts(settings) : [];
    const services = [['__default__',ownDefault() ? '沿用上次' : '全局默认'],['claude','Claude'],['codex','Codex'],['antigravity','Antigravity']].map(([key,label]) =>
      `<button type="button" data-create-provider="${key}" aria-pressed="${provider === key}">${label}</button>`).join('');
    const chosen = selectedModel(settings);
    const missing = settings.override && settings.model && !chosen ? `<option value="${esc(settings.model)}" selected disabled>当前不可选 · ${esc(settings.model)}</option>` : '';
    const modelOptions = settings.override
      ? `<option value="" ${settings.model ? '' : 'selected'}>请选择模型</option>${models.map(model => `<option value="${esc(model.id)}" ${settings.model === model.id ? 'selected' : ''} ${model.available === false ? 'disabled' : ''}>${esc(model.name || modelLabel(model.id))}${model.available === false ? ' · 未就绪' : ''}</option>`).join('')}${missing}`
      : `<option value="__default__">${ownDefault() ? '沿用上次的选择' : '跟随全局默认'}</option>`;
    const effortOptions = `<option value="">模型默认</option>${efforts.map(value => `<option value="${esc(value)}" ${settings.reasoning_effort === value ? 'selected' : ''}>${esc(effortNames[value] || value)}</option>`).join('')}`;
    const serviceState = ai.status?.providers?.[provider];
    const status = !settings.override ? '' : serviceState?.available === false
      ? (serviceState.error || '该服务目前未就绪，请检查连接。')
      : !models.some(model => model.available !== false) ? (models.length ? '该服务目前未就绪，请检查连接。' : '该服务暂无可选模型。')
      : serviceState?.available === true ? `${provider === 'antigravity' ? '服务已配置' : '客户端已登录'}，实际能否调用以请求结果为准。` : '';
    return `<div class="ai-provider-pick"><span>本次使用</span><div class="seg" role="group" aria-label="本次写稿模型服务">${services}</div></div><div class="ai-model-pick"><label class="field"><span>模型</span><select id="ai-create-model" ${settings.override ? '' : 'disabled'}>${modelOptions}</select></label><label class="field"><span>思考强度</span><select id="ai-create-effort" ${settings.override && efforts.length ? '' : 'disabled'}>${settings.override ? effortOptions : '<option>跟随全局默认</option>'}</select></label></div>${status ? `<p class="ai-options-note" role="status">${esc(status)}</p>` : ''}${defaultsMarkup('ai-create')}`;
  }
  function renderCreateModel(modal) {
    const host = $('#ai-create-defaults');if (!host || !modal) return;
    if (modal.action !== 'variants') {host.innerHTML = defaultsMarkup('ai-create');return;}
    const chosen = modal.settings.override ? selectedModel(modal.settings) : null;
    const summary = chosen ? `${chosen.name || modelLabel(chosen.id)}${modal.settings.reasoning_effort ? ` · 思考强度：${effortNames[modal.settings.reasoning_effort] || modal.settings.reasoning_effort}` : ''}（本次单独指定）` : defaultsSummary();
    host.innerHTML = `<details class="ai-create-model-fold" ${modal.modelOpen || (modal.settings.override && !chosen) ? 'open' : ''}><summary><span>模型：<strong>${esc(summary)}</strong></span><span class="ai-create-model-change">改</span></summary>${createModelMarkup(modal.settings)}</details>`;
    host.querySelector('.ai-create-model-fold').addEventListener('toggle',event => {modal.modelOpen = event.target.open;});
    const settings = modal.settings, pick = $('#ai-create-model'), effort = $('#ai-create-effort');
    host.querySelectorAll('[data-create-provider]').forEach(button => {button.onclick = () => {
      const provider = button.dataset.createProvider;
      if (provider === '__default__') {settings.override = false;settings.provider = null;settings.model = null;settings.reasoning_effort = null;}
      else if (!settings.override || (settings.provider || providerOf(selectedModel(settings) || {id:settings.model})) !== provider) {
        settings.override = true;settings.provider = provider;settings.model = null;settings.reasoning_effort = null;
      }
      renderCreateModel(modal);updateCreateControls();host.querySelector(`[data-create-provider="${provider}"]`)?.focus();
    };});
    if (pick) pick.onchange = () => {
      if (settings.override) {
        const previous = settings.reasoning_effort;settings.override = true;settings.model = pick.value;
        const allowed = allowedEfforts(settings), fallback = ai.options?.default_settings?.reasoning_effort;
        settings.reasoning_effort = allowed.includes(previous) ? previous : allowed.includes(fallback) ? fallback : null;
      }
      renderCreateModel(modal);updateCreateControls();$('#ai-create-model')?.focus();
    };
    if (effort && settings.override) effort.onchange = () => {settings.reasoning_effort = effort.value || null;updateCreateControls();};
    const locked = modal.busy || ai.pending.has(modal.key);
    host.querySelectorAll('[data-create-provider]').forEach(button => {button.disabled = locked;});
    if (pick) pick.disabled = locked || !settings.override;if (effort) effort.disabled = locked || !settings.override || !allowedEfforts(settings).length;
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
    if (String(id || '').startsWith('antigravity:')) return `Antigravity · ${id.slice('antigravity:'.length)}`;
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
  // Same four as markets.py on the server, which rejects anything else.
  const writingMarkets = [['us','美国 · 英语'],['uk','英国 · 英语'],['de','德国 · 德语'],['es','西班牙 · 西语']];
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
    const modal = {key,caseId:item.case_id,product,directionId:pending?.payload.direction_id || item.direction_id || '',caseTitle:item.current?.title || item.title || item.direction_id || item.case_id || `${product} · 空白写稿`,revision:pending?.payload.revision || item.revision,planId:pending?.payload.plan_id || item.plan_id || '',action,instruction:pending ? pending.payload.instruction || '' : item.plan_note || '',includeBasket:pending ? !!(pending.payload.include_basket || pending.payload.material_ids?.length) : !!(materialIds?.length || item.direction_id),materialIds,materials:[],directions:item.direction_id ? [item] : [],variantSel:pending?.payload.variants ? pending.payload.variants.map(item => item.index) : pending ? [] : options.variantIndex !== undefined ? [options.variantIndex] : null,newVariants:pending?.payload.new_variant_count || 0,market:pending?.payload.market || 'us',sourceMode:pending?.payload.writing_mode?.mode || 'adapt',divergeCount:pending?.payload.writing_mode?.count || 3,settings:{skill_selection:pending?.payload.skill_selection || 'auto',override:!!pending && Object.hasOwn(pending.payload,'model'),model:pending?.payload.model ?? null,reasoning_effort:pending?.payload.reasoning_effort ?? null},busy:false,blocked:verifyVariant,error:pending ? '此入口上次提交的结果尚未确认。请重试同一请求核对，不会重复创建任务。' : ''};
    ai.modal = modal;modal.blank = item.blank === true;
    modal.bench = {on:!!item.benchmark_hook,hook:item.benchmark_hook || null,contrastId:'',rules:new Set(),contrasts:[],loaded:false,loading:false,error:''};
    setText('#ai-create-title',heading);setText('#ai-create-case',modal.caseTitle);
    setText('#ai-create-version',item.case_id ? '基于当前案例版本' : item.direction_id ? '基于当前方向定义' : '');
    if ($('#ai-create-advanced')) $('#ai-create-advanced').open = false;
    $('#ai-create-defaults').innerHTML = defaultsMarkup('ai-create');renderCreateModel(modal);
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
    }).catch(() => {if (verifyVariant) modal.error = '暂时无法核对这条变体，请关闭窗口后重试。';})]);if (ai.modal !== modal) return;renderBasketChoice(modal);renderSettings('ai-create',modal.settings);renderCreateModel(modal);updateCreateControls();$('#ai-create-instruction').focus();
  }
  function renderBasketChoice(modal) {
    const host = $('#ai-basket-choice');if (!host) return;
    // The writing mode sits above the request: it decides whether the request is needed at all.
    let modeHost = $('#ai-source-mode-host');
    if (!modeHost) {$('#ai-create-form .ai-create-ask').insertAdjacentHTML('beforebegin','<div id="ai-source-mode-host"></div>');modeHost = $('#ai-source-mode-host');}
    modeHost.innerHTML = modal.caseId && modal.action === 'variants' ? sourceModeMarkup(modal) : '';
    const blank = modal.blank && modal.action === 'variants';
    const products = blank ? (window.WorkbenchShell?.getProductChoices?.() || []).map(item => typeof item === 'string' ? item : item?.code || item?.id || item?.value).filter(Boolean) : [];
    const productField = products.length > 1 ? `<label class="field"><span>产品</span><select id="ai-create-product">${products.map(code => `<option value="${esc(code)}" ${code === modal.product ? 'selected' : ''}>${esc(code)}</option>`).join('')}</select></label>` : '';
    const directionField = !modal.caseId ? `<label class="field"><span>方向</span><select id="ai-create-direction"><option value="">不指定方向</option>${modal.directions.filter(item => item.status !== 'archived').map(item => `<option value="${esc(item.direction_id)}" ${item.direction_id === modal.directionId ? 'selected' : ''}>${esc(item.direction_id)} · ${esc(item.main_difference)}</option>`).join('')}</select></label>` : '';
    const marketField = modal.action === 'variants' ? `<label class="field"><span>市场</span><select id="ai-create-market">${writingMarkets.map(([id,label]) => `<option value="${id}" ${id === modal.market ? 'selected' : ''}>${label}</option>`).join('')}</select></label>` : '';
    const links = [modal.action === 'variants' ? '<button type="button" class="subtle" id="ai-create-manual">手动录入</button>' : '', !modal.caseId && !modal.materialIds ? '<button type="button" class="subtle" id="ai-create-from-case">改为从案例写稿</button>' : ''].filter(Boolean);
    host.innerHTML = `${productField || directionField || marketField ? `<div class="ai-source-grid">${productField}${directionField}${marketField}</div>` : ''}<label class="ai-check"><input id="ai-include-basket" type="checkbox" ${modal.includeBasket ? 'checked' : ''} ${ai.pending.has(modal.key) ? 'disabled' : ''}> 带上${modal.materialIds ? '已选择的素材' : '摘录篮里的素材'}（${modal.materialIds?.length ?? modal.materials.length} 条）<small>${esc(modal.basketError ? `摘录篮暂不可用：${modal.basketError}。可取消携带素材，或关闭后重试。` : '提交时固定，之后改摘录篮不影响')}</small></label><div id="ai-variant-plan"></div>${links.length ? `<p class="ai-source-links"><span>已有写好的稿？</span>${links.join('<span aria-hidden="true">·</span>')}</p>` : ''}`;
    $('#ai-include-basket').onchange = event => {modal.includeBasket = event.target.checked;updateCreateControls();};
    if ($('#ai-create-market')) $('#ai-create-market').onchange = event => {modal.market = event.target.value;};
    // Another product reloads its directions and materials, so the window reopens for it and keeps what was typed.
    if ($('#ai-create-product')) $('#ai-create-product').onchange = event => {const code = event.target.value, typed = $('#ai-create-instruction').value;if (modal.busy || code === modal.product) return;closeCreate();void startBlank(code,typed);};
    $$('[name="ai-source-mode"]',modeHost).forEach(radio => {radio.onchange = () => {modal.sourceMode = radio.value;$('#ai-diverge-count').disabled = modal.sourceMode !== 'diverge';updateCreateControls();};});
    if ($('#ai-diverge-count')) $('#ai-diverge-count').oninput = event => {modal.divergeCount = Math.max(1,Math.min(5,Math.floor(Number(event.target.value) || 1)));updateCreateControls();};
    if ($('#ai-create-direction')) $('#ai-create-direction').onchange = event => {modal.directionId = event.target.value;modal.variantSel = null;renderVariantPlan(modal);updateCreateControls();};
    if ($('#ai-create-from-case')) $('#ai-create-from-case').onclick = () => {if (modal.busy) return;closeCreate();void window.WorkbenchShell?.chooseWritingCase();};
    if ($('#ai-create-manual')) $('#ai-create-manual').onclick = () => {if (modal.busy || ai.pending.has(modal.key)) return;closeCreate();void window.CaseScripts?.openManual();};
    renderVariantPlan(modal);
    renderBenchmark(modal);void loadBenchmark(modal);
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
  // 按对标: rules ticked from one saved 对照拆解 and/or one opening from the 开头库, frozen with the task as hard constraints.
  function benchmarkActive(modal) {const b = modal.bench;return !!b?.on && ((b.contrastId && b.rules.size > 0) || !!b.hook);}
  function benchmarkPayload(modal) {
    if (!benchmarkActive(modal)) return {};
    const b = modal.bench;
    return {benchmark:{...(b.contrastId && b.rules.size ? {contrast_id:b.contrastId,rules:[...b.rules].sort((x,y) => x - y)} : {}),...(b.hook ? {hook_id:b.hook.video_id} : {})}};
  }
  async function loadBenchmark(modal) {
    if (modal.bench.loading || modal.bench.loaded) return;
    modal.bench.loading = true;
    try {modal.bench.contrasts = (await api(`/api/benchmark/contrasts?product=${encodeURIComponent(modal.product)}`)).contrasts || [];}
    catch {modal.bench.contrasts = [];modal.bench.error = '对照拆解没读出来，可以先不按对标写。';}
    modal.bench.loading = false;modal.bench.loaded = true;
    if (ai.modal === modal) {renderBenchmark(modal);updateCreateControls();}
  }
  function renderBenchmark(modal) {
    let host = $('#ai-benchmark-host');
    if (!host) {$('#ai-create-form .ai-create-ask').insertAdjacentHTML('beforebegin','<div id="ai-benchmark-host"></div>');host = $('#ai-benchmark-host');}
    if (modal.action !== 'variants') {host.innerHTML = '';return;}
    const b = modal.bench, contrast = (b.contrasts || []).find(item => item.id === b.contrastId);
    const day = value => String(value || '').slice(5, 10).replace('-', '/');
    const none = b.loaded && !(b.contrasts || []).length && !b.hook;
    host.innerHTML = `<section class="ai-benchmark${b.on ? ' is-on' : ''}">
      <label class="ai-benchmark-switch"><span><strong>按对标</strong><small>${none ? '还没有存过对照拆解：在 TK 看板「已保存的结果」做对照拆解后点「存到工作台」，或在开头库挑一个开头。' : '把出圈视频和对照视频的差别当硬条件，写完逐条核对'}</small></span><input type="checkbox" role="switch" id="ai-benchmark-on" ${b.on ? 'checked' : ''} ${none ? 'disabled data-locked="1"' : ''}></label>
      ${b.on ? `<div class="ai-benchmark-body">
        ${(b.contrasts || []).length ? `<label class="field"><span>对标来源</span><select id="ai-benchmark-source"><option value="">不用对照拆解</option>${b.contrasts.map(item => `<option value="${esc(item.id)}" ${item.id === b.contrastId ? 'selected' : ''}>${esc(item.title || '对照拆解')} · ${esc(day(item.created_at))}${item.hit != null ? ` · 出圈 ${item.hit} / 对照 ${item.ctrl}` : ''}</option>`).join('')}</select></label>` : ''}
        ${contrast ? `<fieldset class="ai-benchmark-rules"><legend>这次要做到</legend>${contrast.rules.map(rule => `<label><input type="checkbox" data-benchmark-rule="${rule.n}" ${b.rules.has(rule.n) ? 'checked' : ''}><span><span>${esc(rule.text)}</span><small>${esc(rule.basis || rule.dim)}</small></span></label>`).join('')}</fieldset>` : ''}
        ${b.hook ? `<div class="ai-benchmark-hook"><span><small>参考开头 · ${esc(b.hook.type || '')}</small>@${esc(b.hook.author || '')}：${esc((b.hook.lines?.[0]?.en) || (b.hook.lines?.[0]?.zh) || '')}</span><button type="button" class="subtle" id="ai-benchmark-hook-clear">不用</button></div>` : ''}
        ${b.error ? `<p class="ai-muted">${esc(b.error)}</p>` : ''}
      </div>` : ''}
    </section>`;
    $('#ai-benchmark-on').onchange = event => {b.on = event.target.checked;if (b.on && !b.contrastId && b.contrasts?.length && !b.hook) {b.contrastId = b.contrasts[0].id;b.rules = new Set(b.contrasts[0].rules.map(rule => rule.n));}renderBenchmark(modal);updateCreateControls();};
    if ($('#ai-benchmark-source')) $('#ai-benchmark-source').onchange = event => {b.contrastId = event.target.value;const picked = (b.contrasts || []).find(item => item.id === b.contrastId);b.rules = new Set(picked ? picked.rules.map(rule => rule.n) : []);renderBenchmark(modal);updateCreateControls();};
    $$('[data-benchmark-rule]',host).forEach(box => {box.onchange = () => {const n = Number(box.dataset.benchmarkRule);box.checked ? b.rules.add(n) : b.rules.delete(n);updateCreateControls();};});
    if ($('#ai-benchmark-hook-clear')) $('#ai-benchmark-hook-clear').onclick = () => {b.hook = null;renderBenchmark(modal);updateCreateControls();};
  }
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
  async function startFromHook(hook) {
    const product = hook?.product || window.WorkbenchShell?.product();
    if (!product || !hook?.video_id) return false;
    await openCreate({product,title:`${product} · 按参考开头写稿`,plan_note:'',blank:true,benchmark_hook:hook},'variants','开始写稿');return !!ai.modal;
  }
  async function startBlank(product = window.WorkbenchShell?.product(),instruction = '') {
    if (!product) return pickWritingProduct();
    await openCreate({product,title:`${product} · 空白写稿`,plan_note:instruction,blank:true},'variants','开始写稿');return !!ai.modal;
  }
  // Under 全部产品 the top bar names no product; the reader picks one here and the writing window opens for it.
  function pickWritingProduct() {
    const products = (window.WorkbenchShell?.getProductChoices?.() || []).map(item => typeof item === 'string' ? item : item?.code || item?.id || item?.value).filter(Boolean);
    if (!products.length) {toast('请先选择一个具体产品，再开始空白写稿。',true);return false;}
    const dialog = document.createElement('dialog');dialog.className = 'ai-product-pick';dialog.setAttribute('aria-labelledby','ai-product-pick-title');
    dialog.innerHTML = `<h2 id="ai-product-pick-title">给哪个产品写稿？</h2><p class="context-note">顶栏是「全部产品」，先选一个；顶栏不会跟着改。</p><div class="ai-product-pick-list">${products.map(code => `<button type="button" class="secondary" data-pick-product="${esc(code)}">${esc(code)}</button>`).join('')}</div><div class="ai-product-pick-foot"><button type="button" class="subtle" data-pick-cancel>取消</button></div>`;
    document.body.append(dialog);
    dialog.addEventListener('close',() => dialog.remove());
    dialog.addEventListener('click',event => {
      const pick = event.target.closest('[data-pick-product]');
      if (pick) {dialog.close();void startBlank(pick.dataset.pickProduct);return;}
      if (event.target.closest('[data-pick-cancel]') || event.target === dialog) dialog.close();
    });
    dialog.showModal();dialog.querySelector('[data-pick-product]')?.focus();
    return true;
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
    setText('#ai-create-defaults-summary',modal.settings.override ? '本次单独选择模型，只用于这次写稿，不改全局默认 ·' : defaultsSummary());
    // The folded settings open by themselves when something there needs attention.
    const issue = !pending && settingsIssue(modal.settings) && ai.options && !ai.optionsLoading;
    setText('#ai-create-settings-summary',issue ? '需要处理' : ai.status?.available === false ? 'AI 暂不可用' : '');
    $('#ai-create-settings-summary')?.classList.toggle('is-warn',!!issue || ai.status?.available === false);
    if ((issue || ai.status?.available === false) && $('#ai-create-advanced')) $('#ai-create-advanced').open = true;
    $('#ai-create-availability').classList.toggle('unavailable',ai.status?.available === false);
    $('#ai-create-instruction').readOnly = busy || !!pending;
    const plan = planActive(modal), diverge = divergeActive(modal), optional = plan || diverge || benchmarkActive(modal), selected = (modal.variantSel || []).length;
    $$('#ai-benchmark-host input, #ai-benchmark-host select, #ai-benchmark-host button').forEach(control => {control.disabled = busy || !!pending || control.dataset.locked === '1';});
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
    document.querySelectorAll('#ai-create-defaults [data-create-provider]').forEach(button => {button.disabled = busy || !!pending;});
    if ($('#ai-create-model')) $('#ai-create-model').disabled = busy || !!pending || !modal.settings.override;
    if ($('#ai-create-effort')) $('#ai-create-effort').disabled = busy || !!pending || !modal.settings.override || !allowedEfforts(modal.settings).length;
    $('#ai-create-check').disabled = busy;
    setText('#submit-ai-create',busy ? '正在提交…' : pending ? '重试同一请求' : ai.modal?.action === 'variants' ? '开始写稿' : '开始协作');
    setText('#ai-create-error',modal.error);$('#ai-create-error').hidden = !modal.error;
    setText('#ai-create-hint',pending ? '为防止重复创建，重试使用上次的相同要求和请求。' : diverge ? `将以原稿为底发散 ${modal.divergeCount} 条，每条推进结构不同；提交后可在「内容创作」查看进度与结果。` : plan ? `将按 ${selected} 条变体各写一稿${modal.newVariants ? `，另提 ${modal.newVariants} 条新变体` : ''}；提交后可在「内容创作」查看进度与结果。` : '提交后可在「内容创作」查看进度与结果。');
  }
  function closeCreate() {
    if (ai.modal?.busy) {toast('正在提交，请等待返回结果。',true);return;}
    ai.modal = null;$('#ai-create-dialog').close();$('#ai-create-instruction').value = '';
  }
  async function submitCreate(event) {
    event.preventDefault();const modal = ai.modal;
    if (!modal || modal.busy || modal.blocked || !ai.status?.available || (!ai.pending.has(modal.key) && ((modal.includeBasket && !!modal.basketError) || (!modal.instruction.trim() && !planActive(modal) && !divergeActive(modal) && !benchmarkActive(modal)) || settingsIssue(modal.settings)))) return;
    let pending = ai.pending.get(modal.key);
    if (!pending) {pending = {path:'/api/ai/tasks',payload:{request_id:crypto.randomUUID(),...(modal.planId ? {plan_id:modal.planId} : {}),...(modal.caseId ? {case_id:modal.caseId,revision:modal.revision} : {product:modal.product,...(modal.directionId ? {direction_id:modal.directionId} : {})}),...(modal.includeBasket && modal.materialIds ? {material_ids:modal.materialIds} : {include_basket:modal.includeBasket}),action:modal.action,instruction:modal.instruction.trim() || (benchmarkActive(modal) ? '按勾选的对标约束写稿。' : ''),...planPayload(modal),...benchmarkPayload(modal),...(divergeActive(modal) ? {writing_mode:{mode:'diverge',count:modal.divergeCount}} : {}),...(modal.action === 'variants' && modal.market !== 'us' ? {market:modal.market} : {}),skill_selection:modal.settings.skill_selection,...(modal.settings.override ? {model:modal.settings.model,reasoning_effort:modal.settings.reasoning_effort} : {})}};ai.pending.set(modal.key,pending);}
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
    return `<div class="ai-task-entry${task.archived ? ' is-archived' : ''}" data-ai-task-id="${esc(task.task_id)}"><div class="ai-task-select" ${busy ? 'title="任务正在处理，结束后可整理"' : ''}><input type="checkbox" aria-label="选择 ${esc(title)} 的协作记录" ${busy ? 'disabled' : ''}></div><a class="ai-task-card" href="#ai/${encodeURIComponent(task.task_id)}" title="${esc(task.task_id)}"><span class="ai-action-kind">${esc(actions[task.action] || 'AI 协作')}</span><span class="ai-row-main"><strong>${esc(title)}</strong>${detail ? `<small${task.last_error ? ' class="ai-task-reason"' : ''}>${esc(detail)}</small>` : ''}<small class="ai-management-badge">${esc(managementLabel(task))}${task.resolution_note && managementState(task) === 'pending_selection' ? ` · ${esc(task.resolution_note)}` : ''}${task.archived ? ' · 已归档' : ''}${isWriting(task) && task.adopted_script_count ? ` · 已保存 ${task.adopted_script_count} 份采用稿` : ''}</small></span><span class="ai-row-meta"><span>${esc(readableDate(task.updated_at))} 更新${task.round_count > 1 ? ` · ${task.round_count} 轮` : ''}${task.stale ? ' · 案例已更新' : ''}</span></span><span class="ai-status ${esc(task.status)}">${esc(statusText(task))}</span></a></div>`;
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
    showDeleteDialog(`将删除 ${ai.deleteSelection.length} 条本地协作记录。`);
  }
  function showDeleteDialog(count) {
    setText('#ai-delete-count',count);
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
      if (!ai.deleteSelection.length) {
        $('#ai-delete-dialog').close();toast(`已删除 ${deleted.size} 条本地协作记录。`);
        if (deleted.has(taskRouteId())) location.hash = window.WorkbenchShell?.parentURL?.() || pageURL('#ai');
      }
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
      $('#content').innerHTML = `${window.ContentFlow?.tabs?.('ai') || `<a class="back-link" href="${esc(window.WorkbenchShell?.listURL?.('content') || pageURL('#content'))}">${icon('back')}返回内容列表</a>`}<div class="page-heading ai-list-heading"><div><h1>协作记录</h1><p>查看历次协作、各轮回答及采用记录，归档后仍可回看和恢复。</p></div><button type="button" class="ai-service-pill" id="ai-check-service" title="重新检查 AI 服务"><i></i><span id="ai-service-label">${esc(availabilityText())}</span></button></div>
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
  function usageSummary(usage) {
    return [['input_tokens','输入'],['output_tokens','输出'],['total_tokens','合计'],['cache_read_input_tokens','读缓存'],['cache_creation_input_tokens','写缓存']]
      .filter(([key]) => Number.isSafeInteger(usage?.[key]) && usage[key] >= 0)
      .map(([key,label]) => `${label} ${usage[key].toLocaleString('zh-CN')}`).join(' / ');
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
    const line = [actual,effort === '尚未回报' ? '' : `思考强度：${effort}`,round.skill_selection === 'none' ? '未用技能' : names ? `技能：${names}` : '',round.review_context?.status === 'attached' ? `附评论摘要 ${round.review_context.reviews || '?'} 条` : '',usageSummary(round.usage) ? `Token 用量：${usageSummary(round.usage)}` : ''].filter(Boolean).join(' · ');
    return `<p class="ai-run-line">${esc(line)}</p>${round.skills_status === 'unavailable' || round.review_context?.status === 'unavailable' ? `<p class="ai-skill-status">${esc(round.skills_status === 'unavailable' ? skillState : reviewContextText(round.review_context))}</p>` : ''}${round.migration_notice ? `<p class="ai-options-note">${esc(round.migration_notice)}</p>` : ''}<details class="ai-execution-details"><summary>本轮设置</summary><p>${esc(skillState)}</p>${round.review_context ? `<p>${esc(reviewContextText(round.review_context))}</p>` : ''}<p>请求模型：${esc(requested)} · 请求思考强度：${esc(round.requested_reasoning_effort ? effortNames[round.requested_reasoning_effort] || round.requested_reasoning_effort : '默认')}</p>${skills.map(skill => `<div><strong>${esc(skill.label || skill.name)}</strong><code>${esc(skill.path || '未记录路径')}</code>${skill.sha256 ? `<small>文件校验：${esc(skill.sha256)}</small>` : ''}</div>`).join('')}</details>${stageMetadata(round)}`;
  }
  function stageMetadata(round) {
    const check = [round.format_check,round.reply_check].map(item => item?.status && item.status !== 'ok' && item.reason ? `<p class="ai-format-check ${esc(item.status)}">${esc(item.reason)}</p>` : '').join('');
    if (!round.stages) return check ? `<div class="ai-stage-status">${check}</div>` : '';
    const rows = ['draft','review','format'].filter(name => round.stages[name]).map(name => {
      const stage = round.stages[name], label = {draft:'初稿',review:'独立内容复核',format:'格式整理（只改排版）'}[name];
      const recovered = round.status !== 'completed' && !active(round) ? stage.output || (stage.messages || []).filter(entry => entry.phase !== 'commentary').map(entry => entry.text || '').join('\n\n') : '';
      return `<p>${label}：${esc(statusText(stage))} · 模型：${esc(stage.model ? modelLabel(stage.model) : '尚未回报')} · 思考强度：${esc(effortNames[stage.reasoning_effort] || stage.reasoning_effort || '尚未回报')}${usageSummary(stage.usage) ? ` · Token 用量：${esc(usageSummary(stage.usage))}` : ''}</p>${recovered ? `<details class="ai-execution-details"><summary>查看保留的${label}文本（本轮未完成）</summary><pre>${esc(recovered)}</pre></details>` : ''}`;
    }).join('');
    const body = `${rows}${round.stages.review ? '<small>内容复核采用独立模型调用，不代表本土达人认证、母语者审核或市场验证。</small>' : ''}`;
    return round.status === 'completed' && !check && Object.values(round.stages).every(stage => stage.status === 'completed') ? `<details class="ai-execution-details"><summary>执行阶段与复核记录</summary><div class="ai-stage-status">${body}</div></details>` : `<div class="ai-stage-status">${body}${check}</div>`;
  }
  // A writing round that is being drafted and reviewed (or stopped part-way) shows its steps and settings in a narrow
  // left column and leaves the rest to the reply. Once candidates are laid out, the candidate list takes that column.
  function stageSpent(stage) {
    const start = Date.parse(stage.started_at || ''), end = Date.parse(stage.completed_at || '');
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '';
    const seconds = Math.round((end - start) / 1000);
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
  function runSteps(round) {
    const busy = active(round), names = {draft:'初稿',review:'独立复核',format:'格式整理'};
    const rows = Object.keys(names).filter(name => round.stages[name]).map(name => {
      const stage = round.stages[name];
      const state = stage.status === 'completed' ? 'done' : stage.status === 'running' ? 'current' : ['failed','cancelled','interrupted'].includes(stage.status) ? 'problem' : 'todo';
      const note = state === 'done' ? stageSpent(stage) || '已完成' : state === 'current' ? '进行中' : state === 'todo' ? (busy ? '排队' : '未进行') : statusText(stage);
      const detail = [stage.model ? `模型：${modelLabel(stage.model)}` : '',stage.reasoning_effort ? `思考强度：${effortNames[stage.reasoning_effort] || stage.reasoning_effort}` : '',
        usageSummary(stage.usage) ? `Token 用量：${usageSummary(stage.usage)}` : '',name === 'review' ? '内容复核采用独立模型调用，不代表本土达人认证、母语者审核或市场验证。' : ''].filter(Boolean).join('\n');
      return `<li class="ai-run-step is-${state}" title="${esc(detail)}"><span class="ai-run-mark" aria-hidden="true"></span><span class="ai-run-name">${names[name]}</span><small>${esc(note)}</small></li>`;
    });
    return `${rows.join('')}<li class="ai-run-step is-${round.status === 'completed' ? 'done' : 'todo'}"><span class="ai-run-mark" aria-hidden="true"></span><span class="ai-run-name">完成</span><small></small></li>`;
  }
  function runMeta(round,open) {
    const model = round.model || round.requested_model, effort = round.reasoning_effort || round.requested_reasoning_effort;
    const skills = (Array.isArray(round.skills) ? round.skills : []).map(skill => skill.label || skill.name).filter(Boolean).join('、');
    const lines = [[model ? modelLabel(model) : '本机默认模型',effort ? `思考强度${effortNames[effort] || effort}` : ''].filter(Boolean).join(' · '),
      round.skill_selection === 'none' ? '未用技能' : skills,round.review_context?.status === 'attached' ? `评论摘要 ${round.review_context.reviews || '?'} 条` : ''].filter(Boolean);
    return `${lines.map(line => `<p>${esc(line)}</p>`).join('')}<button type="button" class="subtle compact" data-run-settings aria-expanded="${open}">${open ? '收起设置与用量' : '本轮设置与用量'}</button>`;
  }
  function paintRunRail(node,round) {
    const rail = $('.ai-run-rail',node), logs = $('.task-activity-logs',node), execution = $('.ai-round-execution',node);
    if (!rail || !logs || !execution) return;
    const on = !!round?.stages && !node.classList.contains('is-desk');
    node.classList.toggle('is-run',on);rail.hidden = !on;
    if (!on) {
      // The candidate desk lists the log under "任务信息", so it goes back to its place in the main column.
      if (rail.contains(logs)) $('.ai-desk-info-slot',node).after(logs);
      return;
    }
    if (!rail.firstElementChild) rail.innerHTML = '<ol class="ai-run-steps"></ol><div class="ai-run-logs"></div><div class="ai-run-meta"></div>';
    const open = execution.classList.contains('is-open');
    [['.ai-run-steps',runSteps(round)],['.ai-run-meta',runMeta(round,open)]].forEach(([selector,markup]) => {
      const part = $(selector,rail);
      if (part.dataset.source !== markup) {part.innerHTML = markup;part.dataset.source = markup;}
    });
    const slot = $('.ai-run-logs',rail);if (logs.parentElement !== slot) slot.append(logs);
    // The settings block is opened from the column, not from its own summary, and is rebuilt whenever the round changes.
    const settings = $(':scope > .ai-execution-details',execution);if (settings && settings.open !== open) settings.open = open;
  }
  function repaintRunRail(node) {const round = ai.task?.rounds?.[Number(node?.dataset.roundIndex)];if (round) paintRunRail(node,round);}
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
    const entry = $('[data-desk-section="titles"]',node);
    if (entry) {entry.hidden = !total;$('small',entry).textContent = total ? String(total) : '';}
    if (!total) {if (panel) panel.hidden = true;return;}
    const source = JSON.stringify(groups);
    if (!panel) {panel = document.createElement('section');panel.className = 'ai-title-lines';$('.ai-round-output',node).after(panel);}
    panel.hidden = false;
    const showOpen = () => {if (node.classList.contains('is-desk')) $('.ai-title-lines-detail',panel).open = true;};
    if (panel.dataset.source === source) {showOpen();return;}
    panel.dataset.source = source;
    panel.innerHTML = `<div class="ai-title-lines-head"><h3>发布文案 <small>${total} 条</small></h3><button type="button" class="subtle" data-copy-all-titles>${icon('copy')}复制全部</button></div><details class="ai-title-lines-detail"><summary>逐条查看与复制</summary>${groups.map(group => `<div class="ai-title-group">${group.heading ? `<h4>${esc(group.heading)}</h4>` : ''}${group.lines.map(line => `<button type="button" class="ai-title-line" data-title-line="${esc(line)}" title="复制这一行"><span>${esc(line)}</span>${icon('copy')}</button>`).join('')}</div>`).join('')}</details>`;
    panel.onclick = event => {
      const line = event.target.closest('[data-title-line]');
      if (line) {copyText(line.dataset.titleLine,'标题行已复制');line.classList.add('copied');setTimeout(() => line.classList.remove('copied'),1200);return;}
      if (event.target.closest('[data-copy-all-titles]')) copyText(groups.flatMap(group => group.lines).join('\n'),'全部标题行已复制');
    };
    showOpen();
  }
  // Every writing answer reads as cards: one per variant, what is read aloud on top and production notes folded.
  // Variants are found after rendering, by "变体 #N" title lines or, without them, by each 「口播（配音用）」 block and the
  // rule or 开头 lines before it. A trailing 「发布前核对」 belongs to the whole round. Anything unrecognised stays as written.
  const scriptStatusNames = {draft:'草稿',adopted:'已采纳',filmed:'已拍摄',published:'已发布',reviewed:'已复盘',archived:'已归档'};
  const textOf = node => (node?.textContent || '').trim();
  const boldLine = node => node.tagName === 'P' && node.children.length === 1 && node.firstElementChild.tagName === 'STRONG' && textOf(node) === textOf(node.firstElementChild);
  const variantNumber = node => (/^H[2-5]$/.test(node.tagName) || boldLine(node)) ? /^\s*(?:[\w-]+\s*[·・]\s*)?(?:建议)?(?:新增|备选)?变体\s*[#＃Vv]?\s*(\d+)/.exec(textOf(node))?.[1] || '' : '';
  const blockText = node => (node?.querySelector('code') || node)?.textContent.trim() || '';
  const sectionLabel = node => boldLine(node) || /^H[2-5]$/.test(node.tagName) ? textOf(node) : /^补拍清单\s*[：:]/.test(textOf(node)) ? '补拍清单' : '';
  const isSpokenLabel = node => /^口播\s*[（(]\s*配音用/.test(sectionLabel(node));
  const openingLine = /^开头(逻辑|画面|屏显)\s*(?:[（(][^）)]*[）)])?\s*[：:]\s*/;
  const isOpening = node => openingLine.test(textOf(node));
  // The check may be its own label line or a paragraph led by it in bold ("**发布前核对**：…").
  const isCheckLabel = node => /^(?:同批|共用)?发布前核对/.test(textOf(node)) && (!!sectionLabel(node) || node.firstElementChild?.tagName === 'STRONG');
  function variantGroups(children) {
    const groups = [];
    for (const node of children) {
      const number = variantNumber(node), last = groups.at(-1);
      // A second title line straight under the first, with the same number, belongs to the same variant.
      if (number && last?.number === number && !last.nodes.length) last.subtitles.push(node);
      else if (number) groups.push({number,heading:node,subtitles:[],nodes:[]});
      else if (last) last.nodes.push(node);
    }
    if (groups.length) return groups;
    // No titles: each spoken block starts after the rule before it, or at the 开头 lines leading into it.
    const spoken = children.flatMap((node,index) => isSpokenLabel(node) ? [index] : []), starts = [];
    spoken.forEach((at,order) => {
      const floor = order ? spoken[order - 1] : -1;
      let start = at;
      for (let index = at - 1;index > floor;index--) if (children[index].tagName === 'HR') {start = index + 1;break;}
      if (start === at) while (start - 1 > floor && /^开头/.test(textOf(children[start - 1]))) start--;
      starts.push(start);
    });
    return starts.map((start,order) => ({number:String(order + 1),heading:null,subtitles:[],nodes:children.slice(start,order + 1 < starts.length ? starts[order + 1] : children.length)}));
  }
  function splitVariants(output) {
    const variants = variantGroups([...output.children]);
    let shared = [];
    const last = variants.at(-1);
    if (last) {
      // Only a check that closes the answer is shared; one followed by more script sections stays with its variant.
      const at = last.nodes.findIndex(isCheckLabel);
      if (at >= 0 && !last.nodes.slice(at + 1).some(node => /口播|中文|意群|补拍|图文|发布文案/.test(sectionLabel(node))))
        {shared = last.nodes.slice(at);last.nodes = last.nodes.slice(0,at);}
    }
    for (const variant of variants) {
      while (variant.nodes.at(-1)?.tagName === 'HR') variant.nodes.pop().remove();
      while (variant.nodes[0]?.tagName === 'HR') variant.nodes.shift().remove();
      const nodes = variant.nodes;
      const spokenAt = nodes.findIndex(isSpokenLabel);
      const chineseLabel = /^(?:对应中文|中文对照)\s*[：:]?/;
      let chineseAt = nodes.findIndex(node => chineseLabel.test(sectionLabel(node)) && !sectionLabel(node).replace(chineseLabel,'').trim());
      // "**中文对照**" with the text on the next line renders as one paragraph led by the bold label.
      const inline = chineseAt < 0 ? nodes.findIndex(node => node.tagName === 'P' && node.firstElementChild?.tagName === 'STRONG' && chineseLabel.test(textOf(node.firstElementChild))) : -1;
      variant.chineseText = inline >= 0 ? textOf(nodes[inline]).replace(chineseLabel,'').trim() : '';
      if (inline >= 0) chineseAt = inline;
      variant.spoken = spokenAt >= 0 && nodes[spokenAt + 1]?.tagName === 'PRE' ? nodes[spokenAt + 1] : null;
      variant.chinese = inline >= 0 ? null : chineseAt >= 0 && /^(?:PRE|P)$/.test(nodes[chineseAt + 1]?.tagName) && !sectionLabel(nodes[chineseAt + 1]) ? nodes[chineseAt + 1] : null;
      // 开头画面 and 开头屏显 may be their own lines or items in a list of notes; list items are lifted out.
      variant.opening = [];
      for (const node of spokenAt < 0 ? [] : nodes.slice(0,spokenAt)) {
        if (isOpening(node)) variant.opening.push(node);
        else if (/^[UO]L$/.test(node.tagName)) for (const item of [...node.children].filter(isOpening)) {
          const line = document.createElement('p');line.innerHTML = item.innerHTML;variant.opening.push(line);item.remove();
        }
      }
      const screen = variant.opening.find(node => /^开头屏显/.test(textOf(node)));
      variant.screen = screen ? textOf(screen).replace(openingLine,'') : '';
      const used = new Set([...variant.opening,variant.spoken,variant.chinese,nodes[spokenAt],nodes[chineseAt]].filter(Boolean));
      // Each label starts a fold; a one-line "补拍清单：…" is its own fold together with a list right under it;
      // loose text joins the fold before it.
      variant.folds = [];
      for (const node of nodes.filter(item => !used.has(item) && !(/^[UO]L$/.test(item.tagName) && !item.children.length))) {
        const label = sectionLabel(node), shots = variant.folds.at(-2);
        if (label === '补拍清单' && !boldLine(node)) variant.folds.push({label,nodes:[node]},{label:'',nodes:[]});
        else if (label) variant.folds.push({label,nodes:[]});
        else if (/^[UO]L$/.test(node.tagName) && shots?.label === '补拍清单' && shots.nodes.length === 1 && !variant.folds.at(-1).nodes.length) shots.nodes.push(node);
        else {if (!variant.folds.length) variant.folds.push({label:'',nodes:[]});variant.folds.at(-1).nodes.push(node);}
      }
      variant.folds = variant.folds.filter(fold => fold.nodes.length).map(fold => ({...fold,label:fold.label || '备注'}));
    }
    return {variants,shared};
  }
  // English and Chinese side by side, line against line when both have the same number of lines.
  function variantLines(variant) {
    const lines = node => blockText(node).split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const english = lines(variant.spoken), chinese = variant.chineseText ? variant.chineseText.split(/\r?\n/).map(line => line.trim()).filter(Boolean) : lines(variant.chinese);
    return {english,chinese,paired:english.length > 0 && english.length === chinese.length};
  }
  // What a 改稿 round changed in the draft it rewrote, line by line; nothing for the drafts it carried over.
  const editRecord = (round,position) => round?.kind === 'manual_edit' && round.manual_edit?.variant_index === position ? round.manual_edit : null;
  function editSummary(record) {
    if (!record) return '';
    const lines = record.lines || [], removed = record.removed || [];
    const fresh = lines.filter(line => !line.source).length, redone = lines.filter(line => line.source && line.translated).length, typed = lines.filter(line => line.source && line.before && !line.translated).length;
    const parts = [redone && `重译 ${redone} 句`,typed && `手改 ${typed} 句`,fresh && `新增 ${fresh} 句`,removed.length && `删掉 ${removed.length} 句`].filter(Boolean);
    return `<div class="ai-edit-summary"><span>人工改稿${parts.length ? ' · ' + parts.join(' · ') : ' · 调了顺序'}</span><small>原稿在上一轮；改过的句子，意群表里的画面要求标了「待更新」。</small>${removed.length ? `<details><summary>删掉的句子</summary>${removed.map(line => `<p>${esc(line.spoken)}<small>${esc(line.zh)}</small></p>`).join('')}</details>` : ''}</div>`;
  }
  function variantScriptPairs(variant,record) {
    const {english,chinese,paired} = variantLines(variant);
    if (paired) {
      const marks = record?.lines?.length === english.length ? record.lines : [];
      return `<div class="ai-variant-pairs">${english.map((line,index) => {
        const mark = marks[index], was = mark?.before, tag = mark && !mark.source ? '新增' : was ? mark.translated ? '重译' : '已改' : '';
        return `<p${tag ? ' class="is-edited"' : ''}>${tag ? `<span class="ai-edit-tag">${tag}</span>` : ''}${esc(line)}${was && was.spoken !== line ? `<small class="ai-variant-was">${esc(was.spoken)}</small>` : ''}</p><p class="ai-variant-zh">${esc(chinese[index])}${was && was.zh !== chinese[index] ? `<small class="ai-variant-was">${esc(was.zh)}</small>` : ''}</p>`;
      }).join('')}</div>`;
    }
    return `<div class="ai-variant-pairs is-blocks"><p>${esc(english.join('\n'))}</p><p class="ai-variant-zh">${esc(chinese.join('\n') || '未提供对应中文')}</p></div>`;
  }
  // An adopted script names its variant by index; older ones without it are matched by "变体 #N" in the title.
  // A draft that a 改稿 round carried over unchanged is still the draft its scripts and orders were made from.
  function draftRounds(round,position) {
    const ids = new Set([round.round_id]);
    for (let current = round;current?.kind === 'manual_edit' && current.manual_edit && current.manual_edit.variant_index !== position && ids.size < 50;) {
      const source = current.manual_edit.source_round_id;
      ids.add(source);current = (ai.task?.rounds || []).find(item => item.round_id === source);
    }
    return ids;
  }
  function variantScripts(round,number,position) {
    const rounds = draftRounds(round,position);
    return (ai.taskScripts?.taskId === ai.task?.task_id ? ai.taskScripts.rows : []).filter(script => rounds.has(script.source?.round_id) &&
      (Number.isInteger(script.source.variant_index) ? script.source.variant_index === position : new RegExp(`变体\\s*[#＃Vv]?\\s*${number}(?!\\d)`).test(script.title || '')));
  }
  function variantScript(round,number,position) {
    const rank = ['draft','adopted','filmed','published','reviewed'];
    return variantScripts(round,number,position).filter(script => !script.archived && script.status !== 'archived').sort((a,b) => rank.indexOf(b.status) - rank.indexOf(a.status))[0] || null;
  }
  // Every order made from this draft, whichever version of its script it used.
  function variantOrders(round,number,position) {
    const orders = new Map();
    for (const script of variantScripts(round,number,position)) for (const use of script.production_usage || []) if (use.kind === 'video') orders.set(use.id,{...use,product:script.product});
    return [...orders.values()];
  }
  function variantOrdersMarkup(orders) {
    return orders.map(order => {
      const {href,status} = window.CaseProduction?.orderSummary?.(order) || {href:'#',status:''};
      return `<div class="ai-variant-order"><span>制作单</span><a href="${esc(href)}">${esc(status)}${order.archived ? ' · 已归档' : ''}</a>${orders.length > 1 ? `<small>${esc(order.id)}</small>` : ''}<button type="button" class="ai-variant-order-delete" data-delete-order="${esc(order.id)}" data-order-product="${esc(order.product || '')}">删除</button></div>`;
    }).join('');
  }
  function deleteVariantOrder(button) {
    window.CaseProduction?.deleteCandidateOrder({id:button.dataset.deleteOrder,product:button.dataset.orderProduct},result => {
      ai.taskListAt = 0;
      if (result.task) updateTask(result.task);
      void window.CaseScripts?.mountTaskAdopted?.($('#ai-task-adopted'),ai.task,{force:true});
    });
  }
  const variantChoice = (round,position) => ai.task?.variant_choices?.[round.round_id]?.picks?.[String(position)] || '';
  // A saved script says how far the draft got; before that, what was decided about it.
  function variantStatus(round,number,position = Number(number) - 1) {
    const best = variantScript(round,number,position), choice = variantChoice(round,position);
    if (best) return `<span class="ai-variant-state is-${esc(best.status)}">${esc(scriptStatusNames[best.status] || best.status)}</span>`;
    return choice === 'do' ? '<span class="ai-variant-state is-do">要做</span>' : choice === 'skip' ? '<span class="ai-variant-state is-skip">不做</span>' : '<span class="ai-variant-state">待定</span>';
  }
  // Pressing the marked choice again clears it. Settling the last open draft finishes the task on the server.
  async function chooseVariant(round,position,button) {
    const task = ai.task, wanted = button.dataset.variantChoice, before = managementState(task);
    button.disabled = true;
    try {
      const result = await api(`/api/ai/tasks/${encodeURIComponent(task.task_id)}/variant-choice`,{method:'POST',body:{round_id:round.round_id,variant_index:position,choice:variantChoice(round,position) === wanted ? '' : wanted}});
      ai.taskListAt = 0;
      updateTask(result.task);
      if (result.task.management_state === 'finished' && before !== 'finished') toast('这一轮的稿子都定了，任务已完结。');
    } catch (error) {toast(message(error),true);}
    finally {button.disabled = false;}
  }
  // The desk's left rail: one row per variant, then the round's shared sections. Whatever is picked here fills the right side.
  function deskRail(variants,round,picked,sections) {
    const rows = variants.map((variant,position) => `<button type="button" class="ai-desk-variant" role="option" data-variant-pick="${esc(variant.number)}" data-structure="${esc(variant.structure)}" aria-selected="${variant.number === picked}" title="${esc(variant.title)}"><span class="ai-desk-variant-name">${esc(variantName(variant))}</span><small>${variant.seconds != null ? `${esc(variant.seconds)} 秒 · ` : ''}<span data-variant-status="${esc(variant.number)}" data-variant-position="${position}">${variantStatus(round,variant.number,position)}</span></small></button>`).join('');
    const links = sections.map(item => `<button type="button" class="ai-desk-section" data-desk-section="${item.key}" ${item.hidden ? 'hidden' : ''}><span>${item.label}</span><small class="${item.warn ? 'is-warn' : ''}">${esc(item.note || '')}</small></button>`).join('');
    return `<p class="ai-desk-rail-title">候选稿 · ${variants.length} 条</p><div class="ai-desk-variants" role="listbox" aria-label="选择候选稿">${rows}</div><div class="ai-desk-sections"><p class="ai-desk-rail-title">本轮资料</p>${links}</div>`;
  }
  const variantName = variant => variant.structure ? `#${variant.number} ${variant.structure}` : variant.title;
  // "变体 #1（已登记：价位对比）", "建议新增变体 V2：先问价" and "202H-S2 · 变体 #3 · 其他：送礼" all reduce to the structure's own name.
  function variantStructure(title) {
    const tail = title.split(/[·・]/).pop().replace(/^\s*(?:建议)?(?:新增|备选)?变体\s*[#＃Vv]?\s*\d+\s*[：:]?/,'').trim();
    return tail.replace(/^[（(]\s*(?:已登记\s*[：:])?\s*(.*?)\s*[）)]$/,'$1').replace(/^其他\s*[：:]/,'').trim();
  }
  // 🔴 lines come after the spoken block of their own variant, so counting those blocks tells whose they are.
  function variantTitleLines(source,count) {
    const found = [];let at = -1;
    for (const raw of String(source || '').split(/\r?\n/)) {
      const line = raw.trim().replace(/^[-*]\s+/,'').replace(/^\d+[.)、]\s*/,'');
      if (/^(?:#{1,5}\s*)?(?:\*\*)?口播\s*[（(]\s*配音用/.test(raw.trim())) at++;
      else if (line.startsWith('🔴') && at >= 0) (found[at] ||= []).push(line.replace(/\*\*/g,'').replace(/`/g,'').trim());
    }
    return at + 1 === count ? found : [];
  }
  function clearDesk(output) {
    const node = output.closest('.ai-round');if (!node) return;
    node.classList.remove('is-desk','is-single-draft');delete node.dataset.deskView;
    $('.ai-desk-rail',node).hidden = true;
    repaintRunRail(node);
  }
  function showVariantTab(host,wanted) {
    const label = $$('[data-variant-tab]',host).some(tab => tab.dataset.variantTab === wanted) ? wanted : '口播';
    $$('[data-variant-tab]',host).forEach(tab => tab.setAttribute('aria-selected',String(tab.dataset.variantTab === label)));
    $$('[data-variant-panel]',host).forEach(panel => panel.hidden = panel.dataset.variantPanel !== label);
  }
  const tabName = (label,panel) => label === '开头' ? '开头画面与屏显' : /^第\s*\d+\s*稿$/.test(label) && /^\s*说服链/.test(panel?.textContent || '') ? '说服链与改动' : label;
  const variantTabsMarkup = panels => [...panels.entries()].map(([label,panel]) => `<button type="button" class="ai-variant-tab" role="tab" data-variant-tab="${esc(label)}">${esc(tabName(label,panel))}</button>`).join('');
  function fillVariantPanels(host,panels,variant,lines) {
    const panelFor = label => {
      if (!panels.has(label)) {const panel = document.createElement('div');panel.className = 'ai-variant-panel';panel.dataset.variantPanel = label;host.append(panel);panels.set(label,panel);}
      return panels.get(label);
    };
    if (variant.opening.length) {const panel = panelFor('开头');panel.classList.add('ai-variant-opening');panel.append(...variant.opening);}
    for (const fold of variant.folds) {
      const panel = panelFor(fold.label);
      for (const item of fold.nodes) {
        // Picture data in older candidates stays one more click away.
        if (item.tagName === 'PRE' && /^\s*\{/.test(blockText(item))) {
          const data = document.createElement('details');data.className = 'ai-variant-data';data.innerHTML = '<summary>图文数据（JSON）</summary>';data.append(item);panel.append(data);
        } else panel.append(item);
      }
    }
    if (lines.length) {
      const label = [...panels.keys()].find(name => /发布文案/.test(name)) || '发布文案';
      panelFor(label).insertAdjacentHTML('beforeend',`<div class="ai-variant-title-lines">${lines.map(line => `<button type="button" class="ai-title-line" data-title-line="${esc(line)}" title="复制这一行"><span>${esc(line)}</span>${icon('copy')}</button>`).join('')}</div>`);
    }
  }
  // The desk's tabs on a saved adopted copy: its own words stay on 口播, the rest is the draft as it was written.
  function mountDraftTabs(host,markdown) {
    if (!host?.isConnected || !markdown || $('.ai-variant-tabs',host)) return;
    const lines = variantTitleLines(markdown,1)[0] || [], output = document.createElement('div');
    output.innerHTML = renderMarkdown(displayResponseText(markdown,lines.length > 0));
    const variant = splitVariants(output).variants[0], spoken = document.createElement('div');
    spoken.className = 'ai-variant-panel';spoken.dataset.variantPanel = '口播';spoken.append(...host.childNodes);host.append(spoken);
    const panels = new Map([['口播',spoken]]);
    if (variant?.spoken) fillVariantPanels(host,panels,variant,lines);
    else {const panel = document.createElement('div');panel.className = 'ai-variant-panel markdown-body';panel.dataset.variantPanel = '写稿原文';panel.append(...output.childNodes);host.append(panel);panels.set('写稿原文',panel);}
    if (panels.size < 2) {host.append(...spoken.childNodes);spoken.remove();return;}
    const tabs = document.createElement('div');tabs.className = 'ai-variant-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','这条稿的内容');tabs.innerHTML = variantTabsMarkup(panels);host.prepend(tabs);
    showVariantTab(host,ai.adoptedTab || '口播');
    host.onclick = event => {
      const tab = event.target.closest('[data-variant-tab]');
      if (tab) {ai.adoptedTab = tab.dataset.variantTab;showVariantTab(host,ai.adoptedTab);return;}
      const line = event.target.closest('[data-title-line]');
      if (line) copyText(line.dataset.titleLine,'标题行已复制');
    };
  }
  // 改稿: the draft's lines become rows to rewrite, move, add or drop. A line whose Chinese changed while its spoken
  // words were left alone is spoken again by the writer; a line whose spoken words were typed is saved as typed.
  const editKey = (round,position) => `${ai.task.task_id}:${round.round_id}:${position}`;
  const rowTranslates = row => row.force ?? (row.zh.trim() !== row.origZh && row.spoken.trim() === row.origSpoken);
  function editDraft(variant,round,position) {
    const {english,chinese} = variantLines(variant), record = editRecord(round,position);
    const rows = english.map((spoken,index) => ({source:index + 1,spoken,zh:chinese[index],origSpoken:spoken,origZh:chinese[index],force:null,before:record?.lines?.length === english.length ? record.lines[index]?.before || null : null}));
    // A retranslation that did not finish left what was typed on its own round; it comes back here.
    const lost = (ai.task.rounds || []).at(-1);
    if (lost?.kind === 'manual_edit' && lost.status !== 'completed' && !active(lost) && lost.manual_edit?.source_round_id === round.round_id && lost.manual_edit.variant_index === position)
      return {total:rows.length,restored:true,rows:lost.manual_edit.lines.map(line => {
        const base = rows[line.source - 1];
        return {source:line.source || null,spoken:line.translate ? base?.spoken || '' : line.spoken,zh:line.zh,origSpoken:base?.origSpoken || '',origZh:base?.origZh || '',force:line.translate ? true : null,before:base?.before || null};
      })};
    return {total:rows.length,restored:false,rows};
  }
  function openEdit(card,variant,round,position) {
    if (active(ai.task)) {toast('上一轮还在处理，等它结束再改稿。',true);return;}
    const drafts = (ai.manualDrafts ||= new Map()), key = editKey(round,position);
    let draft = drafts.get(key);
    if (!draft) {draft = editDraft(variant,round,position);drafts.set(key,draft);if (draft.restored) toast('上次没重译成的改动已带回来。');}
    const host = $('.ai-edit',card), script = $('.ai-variant-script',card), language = writingMarkets.find(([id]) => id === ai.task.context?.market)?.[1] || '';
    showVariantTab(card,'口播');
    card.classList.add('is-editing');script.hidden = true;host.hidden = false;
    const close = () => {drafts.delete(key);card.classList.remove('is-editing');host.hidden = true;host.innerHTML = '';script.hidden = false;};
    const changed = row => !row.source || row.spoken.trim() !== row.origSpoken || row.zh.trim() !== row.origZh || rowTranslates(row);
    const paint = () => {
      $$('[data-edit-row]',host).forEach((node,index) => {
        const row = draft.rows[index], again = rowTranslates(row), touched = changed(row);
        node.classList.toggle('is-pending',again);node.classList.toggle('is-changed',touched);
        $('.ai-edit-state',node).textContent = again ? '待重译' : !row.source ? '新增' : touched ? '手改，照你写的存' : '';
        const toggle = $('[data-edit-act=translate]',node);toggle.setAttribute('aria-pressed',String(again));toggle.textContent = again ? '重译 ✓' : '重译';
        const back = $('[data-edit-act=revert]',node), own = row.source && (row.spoken.trim() !== row.origSpoken || row.zh.trim() !== row.origZh || row.force != null);
        back.hidden = !own && !row.before;back.textContent = own ? '还原' : '退回上一版';
        back.title = own ? '放弃这一句刚才的改动' : '换回这一句在上一轮改稿之前的说法';
        $('[data-edit-act=up]',node).disabled = index === 0;$('[data-edit-act=down]',node).disabled = index === draft.rows.length - 1;
        $('[data-edit-act=remove]',node).disabled = draft.rows.length < 2;
      });
      const count = draft.rows.filter(rowTranslates).length, moved = draft.rows.length !== draft.total || draft.rows.some((row,index) => row.source !== index + 1);
      const dirty = moved || draft.rows.some(changed), save = $('[data-edit-save]',host);
      save.disabled = !dirty || !!draft.busy;
      save.textContent = draft.busy ? '正在提交…' : count ? `重译 ${count} 句并存为新一轮` : '存为新一轮';
      $('.ai-edit-foot p',host).textContent = !dirty ? '还没有改动。' : count ? `${count} 句会按上下文重译成达人口吻，其余句子一个字不动。` : '没有要重译的句子，直接保存，不调用 AI。';
    };
    const render = focus => {
      host.innerHTML = `<p class="ai-edit-hint">改右边的中文来理逻辑：改过中文的句子会按整条稿的上下文重译${language ? `（${esc(language)}）` : ''}。想自己定说法，就直接改左边的口播。</p><div class="ai-edit-rows">${draft.rows.map((row,index) => `<div class="ai-edit-row" data-edit-row="${index}"><span class="ai-edit-no">${index + 1}</span><textarea data-edit-field="spoken" rows="2" maxlength="600" aria-label="第 ${index + 1} 句口播" placeholder="留空，按中文重译">${esc(row.spoken)}</textarea><textarea data-edit-field="zh" rows="2" maxlength="600" aria-label="第 ${index + 1} 句中文" placeholder="这一句要说什么">${esc(row.zh)}</textarea><div class="ai-edit-tools"><small class="ai-edit-state"></small><button type="button" data-edit-act="revert"></button><button type="button" data-edit-act="translate" aria-pressed="false" title="中文没改也让这一句按上下文重说一遍；再点一下取消"></button><button type="button" data-edit-act="up" aria-label="上移" title="上移">↑</button><button type="button" data-edit-act="down" aria-label="下移" title="下移">↓</button><button type="button" data-edit-act="add" aria-label="在下面加一句" title="在下面加一句">＋</button><button type="button" data-edit-act="remove" aria-label="删掉这一句" title="删掉这一句">✕</button></div></div>`).join('')}</div><div class="ai-edit-foot"><p></p><button type="button" class="secondary compact" data-edit-cancel>取消</button><button type="button" class="primary compact" data-edit-save></button></div>`;
      paint();
      if (focus != null) $$('[data-edit-field=zh]',host)[focus]?.focus();
    };
    const save = async () => {
      const lines = draft.rows.map(row => ({source:row.source,spoken:row.spoken.trim(),zh:row.zh.trim(),translate:rowTranslates(row)}));
      const blank = lines.findIndex(line => !line.zh), silent = lines.findIndex(line => !line.translate && !line.spoken), count = lines.filter(line => line.translate).length;
      if (blank >= 0) {toast(`第 ${blank + 1} 句还没有中文。`,true);$$('[data-edit-field=zh]',host)[blank]?.focus();return;}
      if (silent >= 0) {toast(`第 ${silent + 1} 句还没有口播，填上或点「重译」。`,true);$$('[data-edit-field=spoken]',host)[silent]?.focus();return;}
      const task = ai.task;
      if (active(task)) {toast('上一轮还在处理，等它结束再保存。',true);return;}
      if (count && !ai.status?.available) {toast('AI 暂时连不上，重译不了；可以先直接改口播保存。',true);return;}
      draft.busy = true;paint();closeStream();ai.mutation++;
      try {
        const response = await api(`/api/ai/tasks/${encodeURIComponent(task.task_id)}/continue`,{method:'POST',body:{request_id:crypto.randomUUID(),task_revision:task.revision,skill_selection:settingsForTask(task).skill_selection,manual_edit:{round_id:round.round_id,variant_index:position,lines}}});
        // The new round opens on the draft that was just rewritten.
        (ai.variantPicks ||= new Map()).set(`${task.task_id}:${response.task.rounds.at(-1).round_id}`,variant.number);
        close();
        if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) updateTask(response.task);else rememberTaskState(response.task);
        toast(count ? `已提交，正在重译 ${count} 句；完成后显示为新一轮。` : '已存为新一轮，原稿还在上一轮。');
      } catch (error) {
        toast(message(error),true);
        if (error.status === 409) void pollTask(true);
      } finally {draft.busy = false;if (!host.hidden) paint();updateTaskControls();syncStream(ai.task);void loadTaskList(0,true).catch(() => {});}
    };
    host.oninput = event => {
      const field = event.target.dataset.editField, row = draft.rows[Number(event.target.closest('[data-edit-row]')?.dataset.editRow)];
      if (!field || !row) return;
      row[field] = event.target.value.replace(/\s*\n\s*/g,' ');
      // Typing the spoken words means they are the person's own; an asked-for retranslation no longer applies.
      if (field === 'spoken') row.force = null;
      paint();
    };
    host.onclick = event => {
      if (event.target.closest('[data-edit-cancel]')) {
        const dirty = draft.rows.length !== draft.total || draft.rows.some((row,index) => row.source !== index + 1 || changed(row));
        if (!dirty || window.confirm('放弃这次改稿？改过的内容不会保存。')) close();
        return;
      }
      if (event.target.closest('[data-edit-save]')) {void save();return;}
      const button = event.target.closest('[data-edit-act]');if (!button || draft.busy) return;
      const index = Number(button.closest('[data-edit-row]').dataset.editRow), row = draft.rows[index], act = button.dataset.editAct;
      if (act === 'translate') {row.force = !rowTranslates(row);if (row.force === (row.zh.trim() !== row.origZh && row.spoken.trim() === row.origSpoken)) row.force = null;paint();return;}
      if (act === 'revert') {
        const own = row.source && (row.spoken.trim() !== row.origSpoken || row.zh.trim() !== row.origZh || row.force != null);
        if (own) Object.assign(row,{spoken:row.origSpoken,zh:row.origZh,force:null});
        else if (row.before) Object.assign(row,{spoken:row.before.spoken,zh:row.before.zh,force:false});
        render();return;
      }
      if (act === 'up' && index > 0) draft.rows.splice(index - 1,2,row,draft.rows[index - 1]);
      else if (act === 'down' && index < draft.rows.length - 1) draft.rows.splice(index,2,draft.rows[index + 1],row);
      else if (act === 'add') {draft.rows.splice(index + 1,0,{source:null,spoken:'',zh:'',origSpoken:'',origZh:'',force:null,before:null});render(index + 1);return;}
      else if (act === 'remove' && draft.rows.length > 1) draft.rows.splice(index,1);
      render();
    };
    render();
  }
  // A retranslation that failed keeps what was typed; this goes back to the draft it was typed on and reopens it.
  function resumeEdit(round) {
    const source = round.manual_edit?.source_round_id, nodes = roundNodes(), node = nodes.find(item => item.dataset.roundId === source);
    if (!node) return;
    (ai.roundPicks ||= new Map()).set(ai.task.task_id,{id:source,count:nodes.length});revealRoundGroup(node);paintRoundFolding();
    const card = $$('.ai-variant-card',node)[round.manual_edit.variant_index];
    if (!card) return;
    $$('[data-variant-pick]',node).find(row => row.dataset.variantPick === card.dataset.variantCard)?.click();
    card.openEdit?.();
  }
  function layoutVariants(output,round) {
    if (output.dataset.layout === output.dataset.source) return;
    output.dataset.layout = output.dataset.source;
    clearDesk(output);
    try {arrangeVariants(output,round);}
    catch (error) {
      // A shape the splitter did not foresee must never cost the reader any text: fall back to the answer as written.
      console.error(error);
      clearDesk(output);
      output.innerHTML = renderMarkdown(displayResponseText(output.dataset.source,true));
      delete output.dataset.variantRound;
    }
  }
  // A task started from 「顺着这条评论写稿」 answers one comment; each of its drafts shows that comment above the opening text.
  const stancePattern = /^[ \t]*顺着这条评论写稿[^\n]*?评论[ \t]+([A-Za-z0-9_-]{1,100})[ \t]*[｜|]/m;
  const stanceId = task => (task?.rounds || []).map(round => stancePattern.exec(String(round.instruction || ''))?.[1]).find(Boolean) || '';
  const stanceReviews = new Map();
  // 「评论贴纸关联：第 N 稿｜评论 ID」 lines after the drafts name the comment each draft opens on; a number named twice with different comments is left out.
  function linkedReviews(text) {
    const found = new Map();
    for (const match of String(text || '').matchAll(/^[ \t>*-]*评论贴纸关联[：:][ \t]*第[ \t]*(\d+)[ \t]*稿[ \t]*[｜|][ \t]*(?:评论[ \t]*)?([0-9a-f]{16})[ \t]*$/gm)) {
      const number = Number(match[1]);
      found.set(number,found.has(number) && found.get(number) !== match[2] ? '' : match[2]);
    }
    return found;
  }
  const stanceName = review => {const handle = String(review.author_handle || '').trim().replace(/^@+/,''), name = String(review.author_name || '').trim();return handle && !/\s/.test(handle) ? `@${handle}` : name || handle || '评论者未记录';};
  function stanceMarkup(review) {
    const name = stanceName(review), face = review.avatar ? `<img class="ai-variant-reply-avatar" src="/api/reviews/avatar/${esc(review.avatar)}" alt="" loading="lazy">` : `<span class="ai-variant-reply-avatar" aria-hidden="true">${esc((name.replace(/^@/,'')[0] || '?').toUpperCase())}</span>`;
    return `<span class="ai-variant-reply-label">回应评论</span><div class="ai-variant-reply-body"><p class="ai-variant-reply-who">${face}<strong>${esc(name)}</strong></p><p class="ai-variant-reply-text" lang="en">${esc(review.text || '')}</p>${window.ReviewZh?.markup({id:review.id,text:review.text,title:review.title,zh:review.zh,title_zh:review.title_zh},'ai-variant-reply-zh') || ''}</div>`;
  }
  function fillStance(slot,id) {
    if (!stanceReviews.has(id)) stanceReviews.set(id,api(`/api/reviews/${encodeURIComponent(id)}`).then(result => result.review).catch(() => {stanceReviews.delete(id);return null;}));
    stanceReviews.get(id).then(review => {
      if (!slot.isConnected) return;
      if (!review) {slot.innerHTML = `<span class="ai-variant-reply-label">回应评论</span><div class="ai-variant-reply-body"><p class="ai-variant-reply-text">评论库里找不到评论 ${esc(id)}</p></div>`;return;}
      slot.innerHTML = stanceMarkup(review);
    });
  }
  function arrangeVariants(output,round) {
    const {variants,shared} = splitVariants(output);
    if (!variants.length || !variants.every(variant => variant.spoken)) return;
    output.dataset.variantRound = round.round_id;
    const estimates = Array.isArray(round.read_estimates) ? round.read_estimates : [];
    variants.forEach((variant,index) => {
      variant.title = variant.heading ? textOf(variant.heading) : variants.length > 1 ? `第 ${variant.number} 条` : '口播稿';
      variant.structure = variant.heading ? variantStructure(variant.title) : '';
      const estimate = estimates.find(item => /(?:[#＃]|\bV)\s*(\d+)/i.exec(item.label || '')?.[1] === variant.number) || (estimates.length === variants.length ? estimates[index] : null);
      variant.seconds = estimate ? estimate.seconds : null;
    });
    const node = output.closest('.ai-round'), several = variants.length > 1, key = `${ai.task.task_id}:${round.round_id}`;
    const picks = (ai.variantPicks ||= new Map()), tabs = (ai.variantTabs ||= new Map()), views = (ai.deskViews ||= new Map());
    if (!variants.some(variant => variant.number === picks.get(key))) picks.set(key,variants[0].number);
    const anchor = variant => variant.heading || variant.nodes[0];
    const titleLines = variantTitleLines(output.dataset.source,variants.length);
    // What the writer put before the first variant; collected now, while the first card is not yet in the way.
    const leading = [];
    for (let item = output.firstElementChild;item && item !== anchor(variants[0]);item = item.nextElementSibling) leading.push(item);
    const currentRound = () => (ai.task?.rounds || []).find(item => item.round_id === round.round_id) || round;
    const stanceDefault = /^[0-9a-f]{16}$/.test(stanceId(ai.task)) ? stanceId(ai.task) : '', links = linkedReviews(output.dataset.source);
    variants.forEach((variant,position) => {
      const stance = links.get(position+1) || stanceDefault;
      const card = document.createElement('article');card.className = 'ai-variant-card';card.dataset.variantCard = variant.number;card.hidden = several && variant.number !== picks.get(key);
      // The line ends with its Chinese gloss in brackets; the gloss goes on a line of its own under the text.
      const screenLine = variant.screen.split(/屏显(?:搭配|备选)\s*[：:]/)[0].trim(), screenGloss = /\s*[（(]([^）)]*)[）)]\s*$/;
      const screen = screenLine.replace(screenGloss,'').trim(), screenChinese = (screenLine.match(screenGloss)?.[1] || '').trim();
      const reviewTarget = window.ScriptReview?.supported(ai.task?.product) ? {kind:'candidate',task_id:ai.task.task_id,round_id:round.round_id,variant_index:position,product:ai.task.product,title:variantName(variant)} : null;
      const reviewKey = reviewTarget ? window.ScriptReview.targetKey(reviewTarget) : '';
      const reviewButton = reviewTarget ? `<button type="button" class="secondary compact script-review-trigger" data-desk-review data-script-review-key="${esc(reviewKey)}" title="用 202H 审稿技能审查这条口播稿（四关体检、红线排雷与逐句画面）">口播审查</button>` : '';
      card.innerHTML = `<div class="ai-variant-card-head"><h4 title="${esc(variant.title)}">${esc(variantName(variant))}</h4><span class="ai-variant-seconds">${variant.seconds != null ? `约 ${esc(variant.seconds)} 秒` : ''}</span><span data-variant-status="${esc(variant.number)}" data-variant-position="${position}">${variantStatus(round,variant.number,position)}</span><div class="ai-variant-choice" role="group" aria-label="这条稿子做不做"><button type="button" data-variant-choice="do" aria-pressed="false">要做</button><button type="button" data-variant-choice="skip" aria-pressed="false">不做</button></div><div class="ai-variant-actions">${reviewButton}<button type="button" class="secondary compact" data-desk-edit>改稿</button><button type="button" class="secondary compact" data-desk-pool title="保存为采用稿、建制作单，并放进「我去拍了」的执行池；留在这页接着看别的稿">采用并入池</button><button type="button" class="primary compact" data-desk-produce title="保存为采用稿并开制作单">采用并开始制作</button><details class="ai-menu ai-variant-more"><summary class="icon-button" aria-label="这条稿的更多操作" title="更多"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" data-copy-variant-spoken>${icon('copy')}复制口播</button><button type="button" role="menuitem" data-desk-adopt>只保存为采用稿</button><button type="button" role="menuitem" data-desk-write-case>写回案例</button></div></details></div></div><div class="ai-variant-orders" hidden></div>${variant.subtitles.map(item => `<p class="ai-variant-subtitle">${esc(textOf(item))}</p>`).join('')}${stance ? `<div class="ai-variant-reply" data-variant-reply="${esc(stance)}"></div>` : ''}${screen ? `<p class="ai-variant-screen" title="${esc(variant.screen)}"><span>开头屏显</span>${esc(screen)}${screenChinese ? `<small class="ai-variant-screen-zh">${esc(screenChinese)}</small>` : ''}</p>` : ''}<div class="ai-variant-tabs" role="tablist" aria-label="这条稿件的内容"></div><div class="ai-variant-panel" data-variant-panel="口播"><div class="ai-variant-script">${editSummary(editRecord(round,position))}<div class="ai-variant-script-labels" aria-hidden="true"><span>口播（配音用）</span><span>对应中文</span></div>${variantScriptPairs(variant,editRecord(round,position))}</div><div class="ai-edit" hidden></div></div>`;
      // Placed before any of its nodes move in, since the first node is also where the card goes.
      anchor(variant).before(card);
      const panels = new Map([['口播',$('[data-variant-panel]',card)]]);
      fillVariantPanels(card,panels,variant,titleLines[position] || []);
      $('.ai-variant-tabs',card).innerHTML = variantTabsMarkup(panels);
      showVariantTab(card,tabs.get(key) || '口播');
      const pick = {variantIndex:position,variantCount:variants.length};
      $('[data-copy-variant-spoken]',card).onclick = () => copyText(blockText(variant.spoken),'口播已复制');
      const edit = $('[data-desk-edit]',card), editable = variantLines(variant).paired;
      edit.disabled = !editable;
      edit.title = editable ? '逐句改中文理逻辑；改过中文的句子按上下文重译，存为新一轮，原稿留在这一轮' : '这条稿的中文没有和口播逐行对应，先在「更多操作」里整理格式';
      card.openEdit = () => openEdit(card,variant,currentRound(),position);
      edit.onclick = card.openEdit;
      if (ai.manualDrafts?.has(editKey(round,position))) card.openEdit();
      const replySlot = $('[data-variant-reply]',card);if (replySlot) fillStance(replySlot,stance);
      if (reviewTarget) {
        $('[data-desk-review]',card)?.addEventListener('click',() => window.ScriptReview?.open(reviewTarget,{
          onApplyRevision: text => {
            const input = $('#ai-continue-instruction');if (!input) return;
            ai.continueAll = false;paintContinueTarget();
            input.value = text;
            input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();input.scrollIntoView({block:'center',behavior:'smooth'});
            toast('已把审查改稿要点填入下面的继续修改框，确认后提交。');
          },
          onAdopted: (result,{pool}) => {
            const task = result.task, newest = task?.rounds?.at(-1);
            if (!task || !newest) return;
            // The new round opens on the draft the review was adopted into.
            (ai.variantPicks ||= new Map()).set(`${task.task_id}:${newest.round_id}`,variant.number);
            if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) updateTask(task);else rememberTaskState(task);
            void loadTaskList(0,true).catch(() => {});
            toast(result.repeated ? '这些改动已经采纳过，打开的是那一轮。' : `已采纳审查：${result.note || '存为新一轮'}，原稿还在上一轮。`);
            if (pool) void window.CaseProduction?.fromCandidate(task,newest,{variantIndex:position,variantCount:variants.length},{pool:true});
          },
        }));
      }
      $('[data-desk-produce]',card).onclick = () => window.CaseProduction?.fromCandidate(ai.task,currentRound(),pick);
      $('[data-desk-pool]',card).onclick = () => window.CaseProduction?.fromCandidate(ai.task,currentRound(),pick,{pool:true});
      $('[data-desk-adopt]',card).onclick = () => window.CaseScripts?.adoptCandidate(ai.task,currentRound(),false,pick);
      $('[data-desk-write-case]',card).onclick = () => window.CaseScripts?.adoptCandidate(ai.task,currentRound(),true,pick);
      card.addEventListener('click',event => {
        if (event.target.closest('.ai-variant-more [role=menuitem]')) event.target.closest('details').open = false;
        const choice = event.target.closest('[data-variant-choice]');
        if (choice) {void chooseVariant(currentRound(),position,choice);return;}
        const removal = event.target.closest('[data-delete-order]');
        if (removal) {deleteVariantOrder(removal);return;}
        const tab = event.target.closest('[data-variant-tab]');
        // The same tab follows from one variant to the next, so shot lists or copy can be compared in turn.
        if (tab) {tabs.set(key,tab.dataset.variantTab);$$('.ai-variant-card',output).forEach(item => showVariantTab(item,tab.dataset.variantTab));return;}
        const line = event.target.closest('[data-title-line]');
        if (line) {copyText(line.dataset.titleLine,'标题行已复制');line.classList.add('copied');setTimeout(() => line.classList.remove('copied'),1200);}
      });
      [variant.heading,...variant.subtitles,...variant.nodes].filter(item => item?.isConnected && !card.contains(item)).forEach(item => item.remove());
    });
    const sections = [];
    const notes = leading.filter(item => item.tagName !== 'HR');
    if (notes.length) {
      const section = document.createElement('section');section.className = 'ai-desk-notes';section.append(...notes);output.prepend(section);
      leading.filter(item => item.isConnected && !section.contains(item)).forEach(item => item.remove());
      const count = section.querySelectorAll(':scope > ul > li, :scope > ol > li').length;
      sections.push({key:'notes',label:'写作说明',note:count ? String(count) : ''});
    }
    // The pre-publish check covers every variant, so it is one section beside them.
    if (shared.length) {
      const section = document.createElement('section');section.className = 'ai-variant-shared';
      const title = sectionLabel(shared[0]), bare = /^(?:同批|共用)?发布前核对\s*[：:]?$/.test(title);
      section.innerHTML = `<h4>${esc(bare ? title.replace(/[：:]$/,'') : '发布前核对')}</h4>`;
      $$('.ai-variant-card',output).at(-1).after(section);
      section.append(...shared.slice(bare ? 1 : 0).filter(item => item.tagName !== 'HR'));
      shared.filter(item => item.isConnected && !section.contains(item)).forEach(item => item.remove());
      const open = /待核|未核|需核/.test(textOf(section));
      sections.push({key:'check',label:'发布前核对',note:open ? '待核' : '',warn:open});
    }
    sections.push({key:'titles',label:'发布文案',hidden:true},{key:'info',label:'任务信息'});
    const rail = $('.ai-desk-rail',node), toTop = () => {
      const top = $('.ai-desk',node).getBoundingClientRect().top, bar = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topbar-h')) || 64;
      if (top < bar) window.scrollBy(0,top - bar - 12);
    };
    rail.innerHTML = deskRail(variants,round,picks.get(key),sections);rail.hidden = false;
    const setView = view => {
      views.set(key,view);node.dataset.deskView = view;
      $$('[data-desk-section]',rail).forEach(item => {if (item.dataset.deskSection === view) item.setAttribute('aria-current','true');else item.removeAttribute('aria-current');});
    };
    const select = number => {
      picks.set(key,number);
      $$('[data-variant-pick]',rail).forEach(row => row.setAttribute('aria-selected',String(row.dataset.variantPick === number)));
      $$('.ai-variant-card',output).forEach(card => card.hidden = card.dataset.variantCard !== number);
      setView('variant');paintContinueTarget();
    };
    rail.onclick = event => {
      const row = event.target.closest('[data-variant-pick]'), section = event.target.closest('[data-desk-section]');
      if (row) {select(row.dataset.variantPick);toTop();}
      else if (section) {setView(section.dataset.deskSection);toTop();}
    };
    rail.onkeydown = event => {
      const row = event.target.closest('[data-variant-pick]');
      if (!row || !/^Arrow(?:Down|Up|Left|Right)$/.test(event.key)) return;
      event.preventDefault();
      (/Down|Right/.test(event.key) ? row.nextElementSibling : row.previousElementSibling)?.focus();
    };
    node.classList.add('is-desk');node.classList.toggle('is-single-draft',!several);
    repaintRunRail(node);
    setView(sections.some(item => item.key === views.get(key)) ? views.get(key) : 'variant');
    paintInfoAttention();repaintVariantStatus();
    void paintBenchmark(output,round);
    void window.ScriptReview?.syncButtons(output);
  }
  // 对标核对: a task written to benchmark rules gets each finished round checked once in the background (benchmark.py);
  // every draft card then shows only its gaps, with what to change, and folds what it already does.
  async function paintBenchmark(output,round) {
    const bench = ai.task?.context?.benchmark, taskId = ai.task?.task_id;
    if (!bench || !taskId || round.status !== 'completed') return;
    const key = `${taskId}:${round.round_id}`, checks = (ai.benchChecks ||= new Map());
    let check = checks.get(key);
    if (!check || check.status === 'running') {
      const query = `/api/benchmark/check?task_id=${encodeURIComponent(taskId)}&round_id=${encodeURIComponent(round.round_id)}`;
      try {
        check = await api(query);
        if (check.status === 'none') check = await api('/api/benchmark/check',{method:'POST',body:{task_id:taskId,round_id:round.round_id}});
      } catch (error) {check = {status:'failed',error:message(error)};}
      checks.set(key,check);
      if (check.status === 'running') setTimeout(() => {if (output.isConnected && ai.task?.task_id === taskId) void paintBenchmark(output,round);},3000);
    }
    if (!output.isConnected) return;
    const rules = check.rules || bench.rules || [], ruleOf = n => rules.find(rule => rule.n === n) || {text:`第 ${n} 条`};
    $$('.ai-variant-card',output).forEach((card,position) => {
      $('.ai-bench-check',card)?.remove();$('.ai-bench-badge',card)?.remove();
      const head = $('.ai-variant-card-head',card);if (!head) return;
      const box = document.createElement('div');box.className = 'ai-bench-check';
      if (check.status === 'running') box.innerHTML = '<p class="ai-bench-note">正在按对标约束逐条核对…</p>';
      else if (check.status !== 'done') box.innerHTML = `<p class="ai-bench-note">对标核对没完成${check.error ? '：' + esc(check.error) : ''}。<button type="button" class="subtle" data-bench-again>再核对一次</button></p>`;
      else {
        const items = (check.results || []).find(result => result.variant === position + 1)?.items || [];
        const gaps = items.filter(item => !item.ok), done = items.filter(item => item.ok);
        head.querySelector('h4')?.insertAdjacentHTML('afterend',`<span class="ai-bench-badge${gaps.length ? ' has-gap' : ''}">对标 ${done.length}/${rules.length}${gaps.length ? ` · ${gaps.length} 个缺口` : ''}</span>`);
        box.innerHTML = (gaps.length ? `<div class="ai-bench-gap" role="note">${gaps.map(item => `<p><strong>缺口：${esc(ruleOf(item.rule).text)}</strong><br>${esc(item.where)}${item.fix ? '。' + esc(item.fix) : ''}</p>`).join('')}
            <div class="ai-bench-actions"><button type="button" class="secondary" data-bench-fix="${position}">按缺口改稿</button></div></div>` : '')
          + (done.length ? `<details class="ai-bench-done"><summary>已做到 ${done.length} 条</summary><ul>${done.map(item => `<li>${esc(ruleOf(item.rule).text)}${item.where ? '：' + esc(item.where) : ''}</li>`).join('')}</ul></details>` : '')
          + (items.length < rules.length ? `<p class="ai-bench-note">有 ${rules.length - items.length} 条 AI 没给结论。</p>` : '');
        box.dataset.gaps = JSON.stringify(gaps.map(item => `- ${ruleOf(item.rule).text}：${item.where}${item.fix ? '。' + item.fix : ''}`));
      }
      head.after(box);
    });
    if (output.dataset.benchBound) return;
    output.dataset.benchBound = '1';
    output.addEventListener('click',async event => {
      const fix = event.target.closest('[data-bench-fix]'), again = event.target.closest('[data-bench-again]');
      if (fix) {
        const lines = JSON.parse(fix.closest('.ai-bench-check')?.dataset.gaps || '[]'), input = $('#ai-continue-instruction');if (!input) return;
        ai.continueAll = false;paintContinueTarget();
        input.value = `按对标核对改这条稿，只动有缺口的句子，其余逐字保留：\n${lines.join('\n')}`;
        input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();input.scrollIntoView({block:'center',behavior:'smooth'});
        toast('已把缺口填进下面的要求，确认后提交。');
      }
      if (again) {
        const current = (ai.task?.rounds || []).find(item => item.round_id === output.dataset.variantRound) || round;
        try {ai.benchChecks.set(`${ai.task.task_id}:${current.round_id}`,await api('/api/benchmark/check',{method:'POST',body:{task_id:ai.task.task_id,round_id:current.round_id,again:true}}));}
        catch (error) {toast(message(error),true);return;}
        void paintBenchmark(output,current);
      }
    });
  }
  // Only the round on screen has a desk; the task's own information and the follow-up box go with it, or back above and below the rounds.
  function visibleDesk() {return roundNodes().find(node => node.classList.contains('is-desk') && !node.classList.contains('is-collapsed') && !node.closest('details:not([open])'));}
  function placeDeskParts() {
    const body = $('#ai-task-info-body'), home = $('#ai-task-info'), composer = $('.ai-composer'), rounds = $('#ai-rounds');
    if (!body || !home || !composer || !rounds) return;
    const desk = visibleDesk();
    home.hidden = !!desk;
    const infoTarget = desk ? $('.ai-desk-info-slot',desk) : home;
    if (body.parentElement !== infoTarget) infoTarget.append(body);
    const main = desk && $('.ai-desk-main',desk);
    if (main ? composer.parentElement !== main : composer.previousElementSibling !== rounds) {
      // Someone may be typing the next request while the round finishes; moving the box must not take the cursor away.
      const box = document.activeElement?.id === 'ai-continue-instruction' ? document.activeElement : null, start = box?.selectionStart, end = box?.selectionEnd;
      if (main) main.append(composer);else rounds.after(composer);
      if (box) {box.focus({preventScroll:true});box.setSelectionRange(start,end);}
    }
    paintContinueTarget();
  }
  function continueTarget() {
    const desk = visibleDesk();
    if (!desk || desk.classList.contains('is-single-draft') || ai.continueAll) return null;
    const row = $('[data-variant-pick][aria-selected="true"]',desk);
    return row ? {number:row.dataset.variantPick,structure:row.dataset.structure || ''} : null;
  }
  function paintContinueTarget() {
    const button = $('#ai-continue-target'), input = $('#ai-continue-instruction');if (!button || !input) return;
    const desk = visibleDesk(), several = !!desk && !desk.classList.contains('is-single-draft'), target = continueTarget();
    button.hidden = !several || (retryable(ai.task) && !input.value.trim());
    if (several) {
      const number = $('[data-variant-pick][aria-selected="true"]',desk)?.dataset.variantPick || '';
      button.textContent = target ? `只改 #${number}` : '改全部变体';button.setAttribute('aria-pressed',String(!!target));
      button.title = target ? '点一下改成针对全部变体' : `点一下改成只针对 #${number}`;
    }
    input.placeholder = retryable(ai.task) ? '无需输入，直接重试上一轮；也可以填写新的修改要求。' : target ? `继续修改 #${target.number}…` : continuationHint(ai.task);
  }
  function paintInfoAttention() {
    const text = ai.infoAttention || '';
    const tag = $('#ai-info-attention');if (tag) {tag.textContent = text;tag.hidden = !text;}
    $$('[data-desk-section="info"] small').forEach(item => {item.textContent = text ? '需核对' : '';item.className = text ? 'is-warn' : '';});
  }
  function repaintVariantStatus() {
    $$('.ai-round-output[data-variant-round]').forEach(output => {
      const round = (ai.task?.rounds || []).find(item => item.round_id === output.dataset.variantRound);
      if (!round) return;
      $$('[data-variant-status]',output.closest('.ai-round')).forEach(cell => {const html = variantStatus(round,cell.dataset.variantStatus,Number(cell.dataset.variantPosition));if (cell.innerHTML !== html) cell.innerHTML = html;});
      $$('.ai-variant-card',output).forEach(card => {
        const position = Number($('[data-variant-status]',card).dataset.variantPosition), choice = variantChoice(round,position);
        card.classList.toggle('has-script',!!variantScript(round,card.dataset.variantCard,position));
        $$('[data-variant-choice]',card).forEach(button => button.setAttribute('aria-pressed',String(button.dataset.variantChoice === choice)));
        const orders = variantOrders(round,card.dataset.variantCard,position), list = $('.ai-variant-orders',card), html = variantOrdersMarkup(orders);
        if (list.dataset.html !== html) {list.dataset.html = html;list.innerHTML = html;}
        list.hidden = !orders.length;
      });
    });
    paintDeskTally();
  }
  // The heading counts the current round's 要做 / 不做 / 还没定, read from the same picks as each card.
  function paintDeskTally() {
    const tag = $('#ai-desk-tally');if (!tag) return;
    const outputs = $$('.ai-round-output[data-variant-round]');
    const output = outputs.filter(node => node.offsetParent !== null).pop() || outputs.pop();
    const round = output && (ai.task?.rounds || []).find(item => item.round_id === output.dataset.variantRound);
    const cards = output ? $$('.ai-variant-card',output) : [];
    if (!round || !cards.length) {tag.hidden = true;return;}
    const picks = cards.map(card => variantChoice(round,Number($('[data-variant-status]',card)?.dataset.variantPosition)));
    const yes = picks.filter(pick => pick === 'do').length, no = picks.filter(pick => pick === 'skip').length, open = picks.length - yes - no;
    tag.textContent = !yes && !no ? `${picks.length} 条都还没定` : open ? `要做 ${yes} · 不做 ${no} · 还没定 ${open}` : `${picks.length} 条都定了 · 要做 ${yes}`;
    tag.dataset.tone = open ? 'warn' : 'ok';tag.hidden = false;
  }
  function taskScriptsLoaded(taskId,rows) {ai.taskScripts = {taskId,rows:Array.isArray(rows) ? rows : []};repaintVariantStatus();}
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
    // Completed rounds already have their canonical reading copy below. Streamed
    // messages can be large; keep them in the record without building a hidden copy.
    if (canonical && !selectionInside(container)) {
      if (container.childNodes.length) container.replaceChildren();
    } else entries.forEach((entry,index) => {
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
      const text = round.output || (round.kind === 'manual_edit' ? active(round) ? '正在按上下文重译改动的句子，完成后这里显示改好的稿子。' : '重译没有完成，你改的内容还在：点下面「回去接着改」会带回来。' : active(round) ? round.stages ? round.active_stage === 'format' ? '正在整理成可制作格式（只改排版，完成后逐字核对）。' : round.active_stage === 'review' ? '初稿已保存，正在独立复核。完成后显示修订候选。' : '正在起草。独立复核完成后显示修订候选。' : '正在等待回复…' : round.kind === 'format' ? '格式整理没有通过，原候选保持不变；可以在原候选里手动拆分口播。' : recovered ? '本轮未完成，已保留的文本可在执行记录中查看。' : '本轮没有返回可用回答。');
      writeResponseBody(output,text,round.status === 'completed' && !!round.output,canonical && isWriting(ai.task));
      if (canonical && isWriting(ai.task) && !selectionInside(output)) layoutVariants(output,round);
    }
    const lost = round.kind === 'manual_edit' && !canonical && !active(round) && ai.task.rounds?.at(-1)?.round_id === round.round_id;
    let resume = $('.ai-edit-resume',node);
    if (lost && !resume) {resume = document.createElement('button');resume.type = 'button';resume.className = 'secondary compact ai-edit-resume';resume.textContent = '回去接着改';output.after(resume);}
    if (resume) {resume.hidden = !lost;resume.onclick = () => resumeEdit(round);}
    const copy = $('.ai-copy-round',node), text = roundCopyText(round);
    updateTitleLines(node,text,round.status === 'completed' && isWriting(ai.task));
    if (isWriting(ai.task)) updateReadEstimates(node,round);
    let raw = $('.ai-raw-output',node);
    if (!raw) {raw = document.createElement('details');raw.className = 'ai-raw-output ai-execution-details';raw.innerHTML = '<summary>查看原始回复与编号</summary><pre></pre>';$('.ai-desk-main',node).append(raw);}
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
    return `${link}<span>基于发起时固定的${task.case_id ? '案例版本' : context.direction_id ? '方向资料' : '创作要求'}</span>${context.writing_mode?.mode === 'diverge' ? `<span>原稿发散 · ${esc(context.writing_mode.count)} 条</span>` : context.writing_mode?.mode === 'replica' ? `<span>照原片优化 · ${esc(context.writing_mode.count)} 条 · 逐句对应 ${esc(context.writing_mode.lines)} 句</span>` : ''}${context.market && context.market !== 'us' ? `<span>目标市场 · ${esc(writingMarkets.find(([id]) => id === context.market)?.[1] || context.market)}</span>` : ''}<details class="ai-version-details"><summary>查看本次资料</summary><div>${task.case_id ? `<span>案例版本</span><code>${esc(task.base_revision || '未提供')}</code><span>来源记录</span><code>${esc(task.base_event_id || '首次原始资料')}</code>` : `<span>产品</span><code>${esc(taskProduct(task))}</code>`}${context.materials?.length ? `<span>随任务固定的素材（${context.materials.length} 条）</span>${context.materials.map(item => `<p>${esc(item.title || item.text || item.source_id || item.kind || '素材')}</p>`).join('')}` : '<span>未附摘录篮条目</span>'}${context.linked_reviews?.length ? `<span>带入的关联评论（${context.linked_reviews.length} 条）</span>${context.linked_reviews.map(item => `<p>${esc(item.linked_as)} · ${esc(item.quote)}</p>`).join('')}` : ''}</div></details>`;
  }
  function roundNodes() {return $$('#ai-rounds .ai-round').sort((a,b) => Number(a.dataset.roundIndex) - Number(b.dataset.roundIndex));}
  function revealRoundGroup(node) {const group = node?.closest('#ai-unused-rounds');if (group) group.open = true;}
  function roundGroupsMarkup(task) {
    if (!isWriting(task)) return '';
    // Left: one row per round, newest first, with 手写新稿 and the cleanup. Right: only the chosen round (or the hand-writing form).
    return `<aside class="ai-round-rail" id="ai-round-rail" aria-label="轮次"><button type="button" class="secondary compact ai-round-write" id="ai-round-write">${icon('plus')}新稿</button><div class="ai-round-rows" id="ai-round-rows"></div><button type="button" class="subtle compact ai-round-cleanup" id="ai-round-cleanup" disabled title="删除没有被采用的轮次，删除后可以撤销">${icon('trash')}一键删除</button><div class="ai-round-cleanup-feedback" id="ai-round-cleanup-feedback" role="status" hidden><span id="ai-round-cleanup-notice"></span><button type="button" class="subtle compact" id="ai-round-cleanup-undo" hidden>撤销删除</button></div></aside><div class="ai-round-stage"><div class="ai-manual-write" id="ai-manual-write" hidden></div><section class="ai-round-group" id="ai-unused-group"><details id="ai-unused-rounds" open><summary><span id="ai-unused-round-count">未采用对话</span></summary><div id="ai-unused-round-list"></div><p class="ai-round-empty" id="ai-unused-round-empty" hidden>暂无未采用对话</p></details></section><section id="ai-kept-group" hidden><p class="ai-kept-round-title">已采用稿与必要来源</p><div id="ai-kept-rounds"></div></section></div>`;
  }
  function roundRowLabel(round) {
    if (active(round)) return {text:statusText(round),cls:'is-running'};
    if (round.status !== 'completed') return {text:statusText(round),cls:'is-failed'};
    return round.cleanup?.protected ? {text:round.cleanup.reason || '已采用',cls:'is-kept'} : {text:'未用',cls:''};
  }
  // The rail mirrors the rounds; choosing a row is the same as opening that round, so nothing else about folding changes.
  function paintRoundRail() {
    const host = $('#ai-round-rows');if (!host || !ai.task) return;
    const nodes = roundNodes(), openNode = nodes.find(node => !node.classList.contains('is-collapsed'));
    const rows = [...(ai.task.rounds || [])].map((round,index) => ({round,index})).filter(item => !item.round.conversation_deleted).reverse();
    const signature = JSON.stringify([rows.map(({round,index}) => [round.round_id,round.status,round.instruction,round.cleanup?.protected,round.cleanup?.reason,round.number || index+1])]);
    host.dataset.empty = rows.length ? '' : 'true';
    if (host.dataset.source !== signature) {
      host.dataset.source = signature;
      host.innerHTML = rows.map(({round,index}) => {
        const label = roundRowLabel(round), summary = String(round.instruction || '').replace(/\s+/g,' ').replace(/^本轮要求\s*[·:：]?\s*/,'').slice(0,40);
        return `<button type="button" class="ai-round-row" data-round-pick="${esc(round.round_id || String(index))}" title="${esc(summary || '')}"><b>${esc(round.number || index+1)}</b><span class="ai-round-row-text">${esc(summary || '（没有要求文字）')}</span><small class="${label.cls}">${esc(label.text)}</small></button>`;
      }).join('');
    }
    const manualOpen = !!$('#ai-manual-write') && !$('#ai-manual-write').hidden;
    $$('.ai-round-row',host).forEach(button => {
      const current = !manualOpen && button.dataset.roundPick === openNode?.dataset.roundId;
      button.classList.toggle('is-current',current);
      if (current) button.setAttribute('aria-current','true');else button.removeAttribute('aria-current');
    });
    $('#ai-round-write')?.classList.toggle('is-current',manualOpen);
  }
  // 手写新稿: a script typed from nothing, saved as a finished round without any AI.
  function manualDraft() {
    const drafts = (ai.manualDrafts ||= new Map());let draft = drafts.get(ai.task.task_id);
    if (!draft) {draft = {open:false,busy:false,error:'',mode:'type',title:'',hook:'',rows:[{spoken:'',zh:''},{spoken:'',zh:''},{spoken:'',zh:''}],paste:{text:'',title:'',preview:null,loading:false,problem:'',seq:0}};drafts.set(ai.task.task_id,draft);}
    return draft;
  }
  // 导入整稿: what the pasted delivery would become, shown before it is saved.
  function importPreviewMarkup(paste) {
    if (!paste.text.trim()) return `<div class="ai-mi-zone" data-mi-zone tabindex="0" role="group" aria-label="粘贴整稿">${icon('file')}<b>Ctrl+V 粘贴整稿</b><span>口播、对应中文、意群表、屏显、补拍清单和发布文案都会带上，不经过 AI。</span><button type="button" class="secondary compact" data-mi-clip>${icon('copy')}从剪贴板粘贴</button></div>`;
    const summary = `<div class="ai-mi-summary"><span>${icon('file')}已贴入 ${paste.text.trim().length.toLocaleString()} 字</span><span></span><button type="button" class="subtle compact" data-mi-raw-toggle aria-expanded="${paste.raw ? 'true' : 'false'}">${paste.raw ? '收起原文' : '查看原文'}</button><button type="button" class="subtle compact" data-mi-clear>重新粘贴</button></div>`;
    if (paste.problem) return `${summary}<p class="ai-error">${esc(paste.problem)}</p>`;
    const found = paste.preview;
    if (!found) return `${summary}<p class="ai-muted">${paste.loading ? '正在识别…' : ''}</p>`;
    if (!found.recognized) {
      const spoken = found.rows.filter(row => row.spoken).length, zh = found.rows.filter(row => row.zh).length;
      return summary + (spoken ? `<div class="ai-mi-fallback"><p>没认出完整的稿子格式（缺「口播（配音用）」）。按行拆出了口播 ${spoken} 句、中文 ${zh} 句，可以填进逐句表格再核对。</p><button type="button" class="secondary compact" data-mi-fill>填进逐句表格</button></div>`
        : '<p class="ai-error">没找到要念的口播。请贴包含「口播（配音用）」的整段稿子。</p>');
    }
    // One chip per part of the delivery: found, found but not lining up, or absent.
    const chip = (state,text) => `<span class="ai-mi-chip is-${state}">${state === 'ok' ? icon('check') : ''}${esc(text)}</span>`;
    return summary + found.variants.map(item => {
      const chips = [chip('ok',`口播 ${item.lines} 句`),
        item.chinese === item.lines ? chip('ok',`中文 ${item.chinese} 句`) : chip('warn',item.chinese ? `中文 ${item.chinese}/${item.lines} 句` : '中文 没对上'),
        item.table === item.lines ? chip('ok',`意群表 ${item.table} 行`) : chip('warn',item.table ? `意群表 ${item.table}/${item.lines} 行` : '意群表 没找到'),
        item.screens ? chip('ok',`屏显 ${item.screens} 条`) : chip('warn','屏显 没找到'),
        item.titles ? chip('ok',`发布文案 ${item.titles} 行`) : chip('warn','发布文案 没找到'),
        item.pickups ? chip('ok',`补拍 ${item.pickups} 条`) : chip('none','补拍清单 无'),
        item.swap ? chip('ok','可换句') : chip('none','可换句 无')];
      return `<div class="ai-mi-variant"><b>${esc(item.label)}</b><p>${esc(item.first)}</p><div class="ai-mi-chips">${chips.join('')}</div>${item.problems.length ? `<ul>${item.problems.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}</div>`;
    }).join('');
  }
  function setImportText(text) {
    const draft = manualDraft(), field = $('#ai-manual-write [data-mi-text]');
    draft.paste.text = text;if (!text.trim()) draft.paste.raw = false;
    if (field && field.value !== text) field.value = text;
    draft.error = '';paintManualWrite();scheduleImportPreview();
    if (!text.trim()) $('#ai-manual-write [data-mi-zone]')?.focus();
  }
  function paintImportPreview() {
    const host = $('#ai-manual-write'), box = host && $('[data-mi-preview]',host);if (!box || !ai.task) return;
    const draft = manualDraft(), markup = importPreviewMarkup(draft.paste);
    if (box.dataset.source !== markup) {box.dataset.source = markup;box.innerHTML = markup;}
    const raw = $('[data-mi-raw]',host);if (raw) raw.hidden = !draft.paste.raw;
    const save = $('[data-mw-save]',host);
    if (save) save.disabled = draft.busy || !draft.paste.preview?.recognized;
  }
  function scheduleImportPreview() {
    const task = ai.task, draft = manualDraft(), paste = draft.paste, seq = ++paste.seq;
    clearTimeout(paste.timer);paste.preview = null;paste.problem = '';paste.loading = !!paste.text.trim();paintImportPreview();
    if (!paste.loading) return;
    paste.timer = setTimeout(async () => {
      try {
        const found = await api('/api/ai/manual-import/preview',{method:'POST',body:{text:paste.text,title:paste.title.trim()}});
        if (seq === paste.seq) paste.preview = found;
      } catch (error) {
        if (seq === paste.seq) paste.problem = message(error);
      } finally {
        if (seq === paste.seq) {paste.loading = false;if (ai.task === task || ai.task?.task_id === task.task_id) paintImportPreview();}
      }
    },300);
  }
  function paintManualWrite(rebuild) {
    const host = $('#ai-manual-write'), rounds = $('#ai-rounds');if (!host || !ai.task) return;
    const draft = manualDraft();
    host.hidden = !draft.open;rounds.classList.toggle('is-manual',draft.open);
    if (!draft.open) {host.innerHTML = '';host.dataset.built = '';return;}
    if (host.dataset.built && !rebuild) {
      const save = $('[data-mw-save]',host), error = $('[data-mw-error]',host);
      save.disabled = draft.busy;save.textContent = draft.busy ? '正在保存…' : '保存为新一轮';
      error.hidden = !draft.error;error.textContent = draft.error;
      if (draft.mode === 'paste') paintImportPreview();
      return;
    }
    host.dataset.built = 'true';
    const head = `<div class="ai-mw-head"><h3>新稿</h3><div class="ai-mw-modes" role="group" aria-label="新稿方式">${[['paste','导入整稿'],['type','逐句手写']].map(([mode,label]) => `<button type="button" data-mw-mode="${mode}" aria-pressed="${draft.mode === mode}">${label}</button>`).join('')}</div></div>`;
    if (draft.mode === 'paste') {
      host.innerHTML = `${head}
      <div class="ai-mi-preview" data-mi-preview aria-live="polite"></div>
      <div class="ai-mi-raw" data-mi-raw ${draft.paste.raw ? '' : 'hidden'}><textarea rows="10" data-mi-text aria-label="整稿原文">${esc(draft.paste.text)}</textarea></div>
      <p class="ai-error" data-mw-error role="alert" ${draft.error ? '' : 'hidden'}>${esc(draft.error)}</p>
      <div class="ai-mw-footer"><input type="text" class="ai-mi-title" data-mi-title maxlength="20" placeholder="稿子名称（可不填）" aria-label="稿子名称" value="${esc(draft.paste.title)}"><span></span><button type="button" class="secondary compact" data-mw-cancel>收起</button><button type="button" class="primary compact" data-mw-save disabled>${draft.busy ? '正在保存…' : '保存为新一轮'}</button></div>`;
      paintImportPreview();return;
    }
    host.innerHTML = `${head}<p class="ai-muted">不经过 AI。每句写口播（目标语言）和对应中文；保存后成为新的一轮，可以像别的候选稿一样采用、改稿、做成制作单。</p>
      <label class="ai-mw-field"><span>稿子名称</span><input type="text" data-mw-title maxlength="60" placeholder="例如：磨刀角度顾虑" value="${esc(draft.title)}"></label>
      <label class="ai-mw-field"><span>开头屏显（可不填）</span><input type="text" data-mw-hook maxlength="200" value="${esc(draft.hook)}"></label>
      <div class="ai-mw-rows">${draft.rows.map((row,index) => `<div class="ai-mw-row" data-mw-row="${index}"><b>${index + 1}</b><textarea rows="2" data-mw-field="spoken" placeholder="口播" aria-label="第 ${index + 1} 句口播">${esc(row.spoken)}</textarea><textarea rows="2" data-mw-field="zh" placeholder="中文" aria-label="第 ${index + 1} 句中文">${esc(row.zh)}</textarea><button type="button" class="subtle compact" data-mw-remove aria-label="删除第 ${index + 1} 句">${icon('close')}</button></div>`).join('')}</div>
      <p class="ai-error" data-mw-error role="alert" ${draft.error ? '' : 'hidden'}>${esc(draft.error)}</p>
      <div class="ai-mw-footer"><button type="button" class="subtle compact" data-mw-add>${icon('plus')}添加一句</button><span></span><button type="button" class="secondary compact" data-mw-cancel>收起</button><button type="button" class="primary compact" data-mw-save ${draft.busy ? 'disabled' : ''}>${draft.busy ? '正在保存…' : '保存为新一轮'}</button></div>`;
  }
  function openManualWrite(mode = ai.manualMode || 'paste') {
    const draft = manualDraft();draft.open = true;draft.error = '';draft.mode = ai.manualMode = mode;
    paintManualWrite(true);paintRoundRail();updateVariantNav();
    $(mode === 'paste' ? '#ai-manual-write [data-mi-zone]' : '[data-mw-title]')?.focus();
  }
  function closeManualWrite() {
    const draft = ai.task && manualDraft();if (!draft?.open) return;
    draft.open = false;paintManualWrite();paintRoundRail();updateVariantNav();
  }
  async function saveManualWrite() {
    const task = ai.task, draft = manualDraft();if (!task || draft.busy) return;
    const fail = text => {draft.error = text;paintManualWrite();};
    if (active(task)) return fail('上一轮还在处理，等它结束再保存。');
    const pasting = draft.mode === 'paste';
    if (pasting && !draft.paste.preview?.recognized) return fail('先贴入能识别的整稿。');
    const rows = pasting ? [{spoken:'-',zh:'-'}] : draft.rows.filter(row => row.spoken.trim() || row.zh.trim()).map(row => ({spoken:row.spoken.trim(),zh:row.zh.trim()}));
    if (!rows.length) return fail('至少写一句。');
    const silent = rows.findIndex(row => !row.spoken), blank = rows.findIndex(row => !row.zh);
    if (silent >= 0) return fail(`第 ${silent + 1} 句还没有口播。`);
    if (blank >= 0) return fail(`第 ${blank + 1} 句还没有中文。`);
    draft.busy = true;draft.error = '';paintManualWrite();closeStream();ai.mutation++;
    try {
      const response = await api(`/api/ai/tasks/${encodeURIComponent(task.task_id)}/continue`,{method:'POST',body:{request_id:crypto.randomUUID(),task_revision:task.revision,...(pasting ? {manual_import:{text:draft.paste.text,title:draft.paste.title.trim()}} : {manual_write:{title:draft.title.trim(),hook:draft.hook.trim(),lines:rows}})}});
      ai.manualDrafts.delete(task.task_id);
      if (pagePath() === `#ai/${encodeURIComponent(task.task_id)}`) updateTask(response.task);else rememberTaskState(response.task);
      toast(pasting ? '整稿已导入为新一轮。' : '手写稿已存为新一轮。');
    } catch (error) {
      draft.error = message(error);
      if (error.status === 409) void pollTask(true);
    } finally {
      draft.busy = false;if ($('#ai-manual-write')) paintManualWrite();
      updateTaskControls();syncStream(ai.task);void loadTaskList(0,true).catch(() => {});
    }
  }
  function bindRoundRail() {
    const rail = $('#ai-round-rail'), host = $('#ai-manual-write');if (!rail || !host) return;
    rail.addEventListener('click',event => {
      const pick = event.target.closest('[data-round-pick]');
      if (pick) {
        const node = roundNodes().find(item => item.dataset.roundId === pick.dataset.roundPick);if (!node) return;
        const draft = manualDraft();draft.open = false;
        (ai.roundPicks ||= new Map()).set(ai.task.task_id,{id:node.dataset.roundId,count:roundNodes().length});
        revealRoundGroup(node);paintRoundFolding();updateVariantNav();return;
      }
      if (event.target.closest('#ai-round-write')) {if (manualDraft().open) closeManualWrite();else openManualWrite();}
    });
    // Ctrl+V anywhere in the panel but a text field takes the whole clipboard as the draft.
    host.addEventListener('paste',event => {
      if (manualDraft().mode !== 'paste' || event.target.closest('input,textarea')) return;
      const text = event.clipboardData?.getData('text/plain') || '';if (!text.trim()) return;
      event.preventDefault();setImportText(text);
    });
    host.addEventListener('input',event => {
      const draft = manualDraft(), row = draft.rows[Number(event.target.closest('[data-mw-row]')?.dataset.mwRow)];
      if (event.target.matches('[data-mi-text],[data-mi-title]')) {
        draft.paste[event.target.matches('[data-mi-text]') ? 'text' : 'title'] = event.target.value;
        if (draft.error) {draft.error = '';paintManualWrite();}
        scheduleImportPreview();
      } else if (event.target.matches('[data-mw-title]')) draft.title = event.target.value;
      else if (event.target.matches('[data-mw-hook]')) draft.hook = event.target.value;
      else if (row && event.target.dataset.mwField) row[event.target.dataset.mwField] = event.target.value.replace(/\s*\n\s*/g,' ');
    });
    host.addEventListener('click',event => {
      const draft = manualDraft(), rowNode = event.target.closest('[data-mw-row]');
      if (event.target.closest('[data-mw-add]')) {draft.rows.push({spoken:'',zh:''});paintManualWrite(true);$$('[data-mw-field=spoken]',host).at(-1)?.focus();}
      else if (event.target.closest('[data-mw-remove]') && rowNode) {
        if (draft.rows.length > 1) draft.rows.splice(Number(rowNode.dataset.mwRow),1);else draft.rows[0] = {spoken:'',zh:''};
        paintManualWrite(true);
      } else if (event.target.closest('[data-mw-mode]')) {
        const mode = event.target.closest('[data-mw-mode]').dataset.mwMode;if (mode !== draft.mode) openManualWrite(mode);
      } else if (event.target.closest('[data-mi-clip]')) {
        void (async () => {
          try {
            const text = await navigator.clipboard.readText();
            if (text.trim()) setImportText(text);else {draft.error = '剪贴板里没有文字，先复制整段稿子。';paintManualWrite();}
          } catch {draft.error = '浏览器没有允许读取剪贴板，直接按 Ctrl+V 粘贴。';paintManualWrite();}
        })();
      } else if (event.target.closest('[data-mi-clear]')) setImportText('');
      else if (event.target.closest('[data-mi-raw-toggle]')) {draft.paste.raw = !draft.paste.raw;paintImportPreview();if (draft.paste.raw) $('[data-mi-text]',host)?.focus();}
      else if (event.target.closest('[data-mi-fill]') && draft.paste.preview?.rows?.length) {
        // No recognisable sections: hand the split lines to the line-by-line form to be checked there.
        draft.rows = draft.paste.preview.rows.map(row => ({spoken:row.spoken,zh:row.zh}));
        draft.title ||= draft.paste.title;draft.hook ||= draft.paste.preview.hook || '';
        openManualWrite('type');
      } else if (event.target.closest('[data-mw-cancel]')) closeManualWrite();
      else if (event.target.closest('[data-mw-save]')) void saveManualWrite();
    });
  }
  function paintRoundCleanup() {
    const task = ai.task, button = $('#ai-round-cleanup');if (!button || !task) return;
    const cleanup = task.round_cleanup, pending = ai.pending.get(`round-cleanup:${task.task_id}`), count = cleanup?.eligible_round_ids?.length || 0;
    const busy = active(task) || !!ai.managing || !!ai.cleaningRounds;
    const deleting = pending?.payload.action === 'delete_unused', undoing = pending?.payload.action === 'undo';
    const unused = $$('#ai-unused-round-list > .ai-round').length;
    setText('#ai-unused-round-count',`未采用对话 · ${unused} 轮`);
    $('#ai-unused-round-empty').hidden = unused > 0;
    $('#ai-kept-group').hidden = !$('#ai-kept-rounds').children.length;
    button.disabled = busy || !!undoing || (!deleting && (!cleanup?.available || !count));
    button.setAttribute('aria-busy',String(!!ai.cleaningRounds && !!deleting));
    const label = deleting ? pending.busy ? '正在删除…' : '重试删除' : count ? `一键删除 · ${count} 轮` : '一键删除';
    if (button.dataset.label !== label) {button.innerHTML = `${icon('trash')}${esc(label)}`;button.dataset.label = label;}
    button.title = active(task) ? '本轮处理结束后可以删除未采用对话。' : !cleanup ? '正在核对采用稿与来源。' : '删除未采用对话，已采用稿及其必要来源保留；删除后可以撤销。';
    const undo = $('#ai-round-cleanup-undo'), batch = cleanup?.undo_batch;
    $('#ai-round-cleanup-feedback').hidden = !batch && !undoing && !cleanup?.deleted_count;
    setText('#ai-round-cleanup-notice',batch ? `已删除 ${batch.count} 轮未采用对话` : cleanup?.deleted_count ? `已删除 ${cleanup.deleted_count} 轮未采用对话` : '正在核对撤销结果');
    undo.hidden = !batch && !undoing;
    undo.disabled = busy || !!deleting || (!undoing && !cleanup?.available);
    undo.setAttribute('aria-busy',String(!!ai.cleaningRounds && !!undoing));
    setText('#ai-round-cleanup-undo',undoing ? pending.busy ? '正在撤销…' : '重试撤销' : '撤销删除');
  }
  async function cleanupRounds(action) {
    const task = ai.task;if (!task || active(task) || ai.managing || ai.cleaningRounds) return;
    const key = `round-cleanup:${task.task_id}`;let pending = ai.pending.get(key);
    if (pending && pending.payload.action !== action) return;
    if (!pending) {
      const cleanup = task.round_cleanup;
      if (!cleanup?.available || (action === 'delete_unused' ? !cleanup.eligible_round_ids?.length : !cleanup.undo_batch)) return;
      pending = {path:`/api/ai/tasks/${encodeURIComponent(task.task_id)}/round-cleanup`,payload:{request_id:crypto.randomUUID(),task_revision:task.revision,action,...(action === 'undo' ? {batch_id:cleanup.undo_batch.id} : {})}};
      ai.pending.set(key,pending);
    }
    pending.busy = true;ai.cleaningRounds = true;closeStream();ai.mutation++;ai.errors.delete(task.task_id);updateTaskControls();
    try {
      const response = await api(pending.path,{method:'POST',body:pending.payload});
      if (response.task?.task_id !== task.task_id) throw new Error('没有收到完整结果，请重试同一请求核对。');
      ai.pending.delete(key);
      if (taskRouteId() === task.task_id) updateTask(response.task);else rememberTaskState(response.task);
    } catch (error) {
      if (definitive(error)) ai.pending.delete(key);
      ai.errors.set(task.task_id,definitive(error) ? message(error) : `操作结果尚未确认：${message(error)}。请重试同一请求。`);
      if (error.status === 409) void pollTask(true);
    } finally {pending.busy = false;ai.cleaningRounds = false;updateTaskControls();syncStream(ai.task);}
  }
  function roundMarkup(round,index) {
    return `<article class="ai-round" data-round-id="${esc(round.round_id || String(index))}" data-round-index="${index}"><div class="ai-round-heading"><button type="button" class="ai-round-toggle" aria-expanded="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg><h2>第 ${esc(round.number || index+1)} 轮</h2><span class="ai-round-summary">${esc(round.instruction || '')}</span></button><span class="ai-round-status ai-status ${esc(round.status)}">${esc(statusText(round))}</span><small class="ai-round-time">${esc(readableDate(round.completed_at || round.created_at))}</small><span class="ai-round-protection" hidden></span></div>
      <div class="ai-round-body"><div class="ai-desk"><nav class="ai-desk-rail" aria-label="候选稿与说明" hidden></nav><aside class="ai-run-rail" aria-label="本轮进度与设置" hidden></aside><div class="ai-desk-main"><details class="ai-user-request" ${String(round.instruction || '').length <= 180 ? 'open' : ''}><summary>本轮要求<span class="ai-request-preview"> · ${esc(String(round.instruction || '').replace(/\s+/g,' ').slice(0,90))}${String(round.instruction || '').length > 90 ? '…' : ''}</span></summary><p>${esc(round.instruction || '')}</p></details><div class="ai-round-execution"></div><div class="ai-desk-info-slot"></div><div class="task-activity-logs"></div>
      <div class="ai-output-heading"><h3 class="ai-output-title">实时回复</h3><div class="ai-output-actions"><button type="button" class="subtle ai-copy-round" hidden>${icon('copy')}复制当前文本</button></div></div><div class="ai-round-messages"></div><div class="ai-round-output markdown-body"></div><p class="ai-round-error ai-error" ${round.error ? '' : 'hidden'}>${esc(typeof round.error === 'string' ? round.error : round.error ? JSON.stringify(round.error) : '')}</p></div></div></div></article>`;
  }
  function renderTaskShell(task,keepScroll) {
    ai.page = task.task_id;
    $('#breadcrumb').innerHTML = '内容创作 <span>/</span> 任务详情';
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || pageURL('#ai'))}">${icon('back')}返回协作记录</a><div class="page-heading ai-task-heading"><div><p class="ai-task-meta"><span class="ai-status" id="ai-task-status"></span><span>${esc(actions[task.action] || 'AI 协作')}</span><span id="ai-archived-tag"></span><span id="ai-desk-tally" class="ai-desk-tally" hidden></span></p><h1>${esc(contextTitle(task))}</h1></div><div class="ai-task-tools"><button type="button" class="secondary compact" id="ai-cancel-task">取消处理</button><label class="ai-task-pick"><span class="sr-only">管理状态</span><select id="ai-management-state">${Object.entries(managementLabels(task)).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select></label><button type="button" class="secondary compact" id="ai-save-management" hidden>重试保存</button><details class="ai-menu" id="ai-task-menu"><summary class="icon-button" aria-label="任务菜单"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" id="ai-copy-full">复制完整原文</button><button type="button" role="menuitem" id="ai-task-archive">归档任务</button><button type="button" role="menuitem" id="ai-task-delete">永久删除任务</button></div></details></div></div>
      <details class="ai-task-info" id="ai-task-info"><summary><span>任务信息</span><small class="is-warn" id="ai-info-attention" hidden></small></summary><div class="ai-task-info-body ai-info-scope" id="ai-task-info-body"><p class="ai-info-kind">${esc(actions[task.action] || 'AI 协作')}</p><div class="ai-task-context">${taskContextMarkup(task)}${experienceContextMarkup(task)}<details class="ai-version-details"><summary>任务编号</summary><code>${esc(task.task_id)}</code><button type="button" class="subtle compact" data-copy-task-id>复制编号</button></details></div></div></details><p id="ai-stale-warning" class="ai-stale-warning" hidden>案例已经更新。这个任务继续使用开始时的案例版本；若要基于新版讨论，请回到案例重新发起任务。</p>
      <div id="ai-task-adopted"></div>${task.action === 'retrospective' ? '<div id="ai-retro-card" class="feedback-chain ai-retro-card" hidden></div>' : ''}
      <div class="ai-task-progress"><p id="ai-task-progress" role="status"></p><small id="ai-stream-status" class="ai-stream-status"></small></div><p class="ai-error" id="ai-task-error" role="alert" hidden></p><div id="ai-rounds">${roundGroupsMarkup(task)}</div>
      <section class="ai-continue-card ai-composer" aria-label="继续讨论"><form id="ai-continue-form"><button type="button" class="ai-continue-target" id="ai-continue-target" hidden></button><label class="sr-only" for="ai-continue-instruction">下一步要求</label><textarea id="ai-continue-instruction" maxlength="12000" rows="2" required placeholder="${continuationHint(task)}"></textarea>
        ${defaultsMarkup('ai-continue')}<details class="ai-advanced" id="ai-continue-advanced"><summary><span>技能与服务</span><span class="ai-advanced-summary" id="ai-continue-settings-summary"></span></summary>${serviceMarkup()}<div id="ai-continue-settings"></div></details>
        <div class="ai-continue-footer"><p class="ai-muted" id="ai-continue-note"></p><small class="ai-shortcut-hint">Ctrl + Enter 提交</small><button type="submit" class="primary compact" id="ai-continue-submit">继续修改</button></div></form></section>
      <nav id="ai-variant-nav" class="ai-variant-nav" aria-label="变体导航" hidden><button type="button" class="ai-variant-top" data-variant-top aria-label="回到顶部"><span class="ai-variant-tip"><b>回到顶部</b></span></button><div class="ai-variant-list"></div></nav>`;
    ai.infoAttention = '';
    window.WorkbenchLinks?.mount({kind:'task',record:task,onAttention:text => {ai.infoAttention = text;paintInfoAttention();}});
    const links = $('#workflow-links');if (links) $('#ai-task-info-body').append(links);
    bindVariantNav();bindRoundRail();
    $('#ai-continue-instruction').value = ai.drafts.get(task.task_id) || ai.pending.get(`continue:${task.task_id}`)?.payload.instruction || '';
    renderSettings('ai-continue',settingsForTask(task));
    $('#ai-continue-instruction').addEventListener('input',event => {ai.drafts.set(task.task_id,event.target.value);updateTaskControls();});
    $('#ai-continue-target').onclick = () => {ai.continueAll = !ai.continueAll;paintContinueTarget();};
    $('#ai-continue-form').addEventListener('submit',submitContinue);$('#ai-cancel-task').onclick = cancelTask;connectServiceButton();
    $('[data-copy-task-id]').onclick = () => copyText(task.task_id,'任务编号已复制');
    $('#ai-management-state').value = managementState(task);
    $('#ai-management-state').dataset.persistedState = managementState(task);
    // Choosing a state saves it. If the save fails for good the list goes back to what is stored; an unconfirmed one offers a retry.
    const saveManagement = async () => {
      const select = $('#ai-management-state'), current = ai.task;
      await manageTasks(`state:${current.task_id}`,[current],{management_state:select.value});
      if (select.isConnected && !ai.pending.has(`manage:state:${current.task_id}`)) select.value = managementState(ai.task);
    };
    $('#ai-management-state').onchange = saveManagement;$('#ai-save-management').onclick = saveManagement;
    if ($('#ai-round-cleanup')) {
      $('#ai-round-cleanup').onclick = () => cleanupRounds('delete_unused');
      $('#ai-round-cleanup-undo').onclick = () => cleanupRounds('undo');
      $('#ai-unused-rounds').addEventListener('toggle',() => {paintRoundFolding();updateVariantNav();});
    }
    $('#ai-copy-full').onclick = () => {
      const id = roundNodes().filter(node => !node.classList.contains('is-collapsed') && !node.closest('details:not([open])')).at(-1)?.dataset.roundId, round = (ai.task.rounds || []).find(item => item.round_id === id), text = round ? roundCopyText(round) : '';
      if (text) copyText(text,'完整原文已复制');else toast('这一轮还没有可复制的文本。',true);
    };
    $('#ai-task-menu').addEventListener('click',event => {if (event.target.closest('[role=menuitem]')) event.currentTarget.open = false;});
    $('#ai-task-archive').onclick = () => manageTasks(`archive:${ai.task.task_id}`,[ai.task],{archived:!ai.task.archived});
    $('#ai-task-delete').onclick = () => {
      if (ai.deleting) return;
      ai.deleteSelection = [{task_id:ai.task.task_id,revision:ai.task.revision}];
      showDeleteDialog('将永久删除这个任务的要求、各轮回复和全部候选稿，不能恢复。已保存的采用稿、制作单、配音和时间线都会保留。');
    };
    // Older rounds fold to one line; a click opens any of them.
    $('#ai-rounds').addEventListener('click',event => {
      const settings = event.target.closest('[data-run-settings]');
      if (settings) {
        const node = settings.closest('.ai-round');
        $('.ai-round-execution',node).classList.toggle('is-open');repaintRunRail(node);
        $('[data-run-settings]',node)?.focus({preventScroll:true});return;
      }
      const toggle = event.target.closest('.ai-round-toggle');
      if (toggle) {
        const node = toggle.closest('.ai-round'), open = node.classList.contains('is-collapsed');
        if (isWriting(ai.task)) (ai.roundPicks ||= new Map()).set(task.task_id,{id:open ? node.dataset.roundId : null,count:roundNodes().length});
        else (ai.openRounds ||= new Map()).set(`${task.task_id}:${node.dataset.roundId}`,open);
        paintRoundFolding();updateVariantNav();return;
      }
      const menu = event.target.closest('.ai-menu');if (menu && event.target.closest('[role=menuitem]')) menu.open = false;
    });
    if (!keepScroll) window.scrollTo(0,0);
  }
  function paintRoundFolding() {
    const nodes = roundNodes(), writing = isWriting(ai.task), picks = (ai.roundPicks ||= new Map());
    let picked = picks.get(ai.task?.task_id);
    // A new round opens automatically; otherwise preserve the reader's choice, including closing every round.
    if (writing && (!picked || picked.count !== nodes.length || (picked.id !== null && !nodes.some(node => node.dataset.roundId === picked.id)))) {
      picked = {id:nodes.at(-1)?.dataset.roundId || null,count:nodes.length};picks.set(ai.task.task_id,picked);
      revealRoundGroup(nodes.at(-1));
    }
    nodes.forEach((node,index) => {
      const chosen = ai.openRounds?.get(`${ai.task?.task_id}:${node.dataset.roundId}`), open = writing ? node.dataset.roundId === picked.id : chosen ?? index === nodes.length - 1;
      node.classList.toggle('is-collapsed',!open);$('.ai-round-toggle',node).setAttribute('aria-expanded',String(open));
    });
    // Historical rounds keep their headings until opened. In particular, a long
    // writing history must not build every hidden candidate desk before showing one.
    nodes.forEach((node,index) => {
      const round = ai.task.rounds.find(item => item.round_id === node.dataset.roundId);
      if (round && !node.classList.contains('is-collapsed') && !node.closest('details:not([open])')) updateRoundMessages(node,round);
    });
    placeDeskParts();
    paintManualWrite();paintRoundRail();
  }
  // Right-edge outline of every "变体 X" heading in the open rounds: fixed on screen, click to jump, one tap back to top.
  function variantHeadings() {
    const open = roundNodes().filter(node => !node.classList.contains('is-collapsed') && !node.closest('details:not([open])')), found = [];
    open.forEach((round,roundIndex) => {
      if ($('.ai-variant-card',round)) return;
      $$('.markdown-body h2,.markdown-body h3,.markdown-body h4,.markdown-body h5,.markdown-body p',round).forEach(heading => {
        if (heading.closest('[hidden]')) return;
        // Variants are usually a bold-only line ("**202H-S2 · 变体 #1 · …**"), not a markdown heading.
        if (heading.tagName === 'P' && !(heading.children.length === 1 && heading.firstElementChild.tagName === 'STRONG' && (heading.textContent || '').trim() === (heading.firstElementChild.textContent || '').trim())) return;
        const match = /^.{0,40}?变体\s*#?\s*([A-Za-z0-9]+|[一二三四五六七八九十]+)/.exec(heading.textContent || '');
        if (!match) return;
        const id = match[1].toUpperCase(), label = `变体${id}`;
        let preview = '', node = heading.nextElementSibling;
        while (node && !preview && !/^H[2-5]$/.test(node.tagName)) {preview = (node.textContent || '').trim().replace(/\s+/g,' ');node = node.nextElementSibling;}
        found.push({heading,label:open.length > 1 ? `${$('.ai-round-toggle h2',round).textContent} · ${label}` : label,title:(heading.textContent || '').trim(),preview:preview.slice(0,90)});
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
    const node = roundNodes().find(item => item.dataset.roundId === round);
    if (!node) return;
    ai.focusedRound = hash;
    (ai.openRounds ||= new Map()).set(`${ai.task.task_id}:${round}`,true);
    (ai.roundPicks ||= new Map()).set(ai.task.task_id,{id:round,count:roundNodes().length});
    revealRoundGroup(node);
    paintRoundFolding();
    if (params.get('adopted')) return true;
    node.tabIndex = -1;node.focus({preventScroll:true});node.scrollIntoView({block:'start'});
    return true;
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
    $('.ai-task-meta').hidden = task.status === 'completed' && !task.archived;
    setText('#ai-archived-tag',task.archived ? '已归档' : '');
    const container = $('#ai-rounds'), rounds = task.rounds || [], keys = new Set(rounds.map((round,index) => String(round.round_id || index)));
    const focused = document.activeElement?.id === 'ai-continue-instruction' ? document.activeElement : null, start = focused?.selectionStart, end = focused?.selectionEnd;
    roundNodes().forEach(node => {
      if (keys.has(node.dataset.roundId)) return;
      const body = $('#ai-task-info-body'), home = $('#ai-task-info'), composer = $('.ai-composer');
      if (body && node.contains(body)) {home.append(body);home.hidden = false;}
      if (composer && node.contains(composer)) container.after(composer);
      node.remove();
    });
    const positions = new Map();
    rounds.forEach((round,index) => {
      const key = String(round.round_id || index);let node = roundNodes().find(child => child.dataset.roundId === key);
      const created = !node;
      if (created) {const holder = document.createElement('div');holder.innerHTML = roundMarkup(round,index);node = holder.firstElementChild;}
      node.dataset.roundIndex = String(index);
      const target = isWriting(task) ? $(round.cleanup?.protected ? '#ai-kept-rounds' : '#ai-unused-round-list') : container, position = positions.get(target) || 0;
      if (target.children[position] !== node) target.insertBefore(node,target.children[position] || null);
      positions.set(target,position+1);
      if (created) window.TaskActivity?.mountLogs($('.task-activity-logs',node),{owner_id:task.task_id,source_id:round.round_id,quiet:true});
      const title = $('.ai-round-toggle h2',node), label = `第 ${round.number || index+1} 轮`;
      if (title.textContent !== label) title.textContent = label;
      const protection = $('.ai-round-protection',node);protection.hidden = !round.cleanup?.protected;
      if (protection.textContent !== (round.cleanup?.reason || '')) protection.textContent = round.cleanup?.reason || '';
      const status = $('.ai-round-status',node);status.className = `ai-round-status ai-status ${round.status}`;if (status.textContent !== statusText(round)) status.textContent = statusText(round);
      const execution = $('.ai-round-execution',node), metadata = roundMetadata(round);
      if (execution.dataset.source !== metadata && !selectionInside(execution)) {execution.innerHTML = metadata;execution.dataset.source = metadata;}
      paintRunRail(node,round);
      const error = $('.ai-round-error',node), reason = typeof round.error === 'string' ? round.error : round.error ? JSON.stringify(round.error) : '';
      error.hidden = !reason;if (error.textContent !== reason) error.textContent = reason;
      const time = $('.ai-round-time',node), value = readableDate(round.completed_at || round.created_at);if (time.textContent !== value) time.textContent = value;
    });
    if (!focusRequestedRound()) paintRoundFolding();
    paintRoundCleanup();
    repaintVariantStatus();
    updateVariantNav();
    if (!active(task)) {ai.cancelWaiting.delete(task.task_id);ai.pending.delete(`cancel:${task.task_id}`);}
    updateTaskControls();syncStream(task);
    if (focused?.isConnected && document.activeElement !== focused) {focused.focus({preventScroll:true});focused.setSelectionRange(start,end);}
  }
  function updateTaskControls() {keepReadingPlace(paintTaskControls);}
  function paintTaskControls() {
    const task = ai.task;if (!task || !$('#ai-continue-form')) return;
    const managementBusy = !!ai.managing || !!ai.cleaningRounds || active(task);
    paintRoundCleanup();
    const stateUnconfirmed = ai.pending.has(`manage:state:${task.task_id}`);
    $('#ai-management-state').disabled = managementBusy || stateUnconfirmed;
    $('#ai-save-management').hidden = !stateUnconfirmed;$('#ai-save-management').disabled = managementBusy;
    $('#ai-task-archive').disabled = managementBusy;
    $('#ai-task-delete').disabled = managementBusy;
    setText('#ai-task-archive',ai.pending.has(`manage:archive:${task.task_id}`) ? '重试整理' : task.archived ? '恢复任务' : '归档任务');
    $('#ai-management-state').title = (task.archived ? '已归档 · 各轮回答与来源关联均保留。继续讨论会恢复为活跃任务。' : active(task) ? '本轮结束后可保存管理状态或归档。' : isWriting(task) ? '管理状态由你确认；归档后可在历史中恢复。' : '仅整理此任务；正式复盘决定或案例修改请回到来源记录。');
    const key = `continue:${task.task_id}`, pending = ai.pending.get(key), cancel = ai.pending.get(`cancel:${task.task_id}`), waiting = task.stopping || ai.cancelWaiting.has(task.task_id), busy = pending?.busy || cancel?.busy || ai.managing || ai.cleaningRounds;
    const input = $('#ai-continue-instruction'), retry = retryable(task);input.readOnly = !!pending;input.required = !retry && !pending;
    $('#ai-continue-form').closest('.ai-composer').classList.toggle('is-retry',retry || !!pending);
    paintContinueTarget();
    $('#ai-continue-submit').disabled = !!busy || (active(task) && !pending) || !ai.status?.available || (!pending && !retry && !input.value.trim()) || (!pending && !!settingsIssue(settingsForTask(task)));
    if ($('#ai-continue-fieldset')) $('#ai-continue-fieldset').disabled = !!pending;
    const continuation = isWriting(task) ? '继续修改' : task.action === 'retrospective' ? '继续复盘' : '继续审稿';
    setText('#ai-continue-submit',pending?.busy ? '正在提交…' : pending ? '重试同一请求' : retry ? input.value.trim() ? continuation + '并重试' : '重试' : continuation);
    const button = $('#ai-cancel-task');button.hidden = !active(task);button.disabled = !!busy || waiting;
    button.textContent = cancel?.busy ? '正在请求取消…' : waiting ? '等待取消完成…' : cancel ? '重试取消请求' : '取消处理';
    setText('#ai-continue-note',pending ? '上次提交尚待核对；重试会使用相同要求，避免重复增加一轮。' : active(task) ? '本轮处理期间可以先写下一步要求，完成后再提交。' : retry ? '无需输入即可重试上一轮，沿用原来的要求；也可以填写新的修改要求后提交。' : '继续使用任务开始时固定的资料，并保留此前每轮记录。');
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
    if (ai.pending.get(key)?.busy || (!ai.pending.has(key) && ((!instruction && !retryable(task)) || settingsIssue(settingsForTask(task))))) return;
    let pending = ai.pending.get(key);
    // Written into the request itself, so the round's record says which variant it was about.
    const target = instruction ? continueTarget() : null, scoped = target ? `本轮只改变体 #${target.number}${target.structure ? `（${target.structure}）` : ''}：${instruction}` : instruction;
    if (!pending) {pending = {path:`/api/ai/tasks/${encodeURIComponent(task.task_id)}/continue`,payload:{request_id:crypto.randomUUID(),task_revision:task.revision,instruction:scoped,skill_selection:settingsForTask(task).skill_selection}};ai.pending.set(key,pending);}
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
    if (Number.isInteger(task.revision) && Number.isInteger(previous.revision) && task.revision !== previous.revision) return task.revision < previous.revision;
    const oldTime = Date.parse(previous.updated_at || ''), newTime = Date.parse(task.updated_at || '');
    if (Number.isFinite(oldTime) && Number.isFinite(newTime) && newTime < oldTime) return true;
    return !task.round_cleanup && (task.rounds?.length || 0) < (previous.rounds?.length || 0);
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
  // Reopen the list with the view and filters it was last left on.
  $('#ai-nav').onclick = () => {const page = window.WorkbenchGuest ? 'ai' : 'content';location.hash = window.WorkbenchShell?.listURL?.(page) || pageURL('#' + page);};
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
  window.addEventListener('workbench:productchange',() => {if (ai.taskCounts) paintTaskCounts(ai.taskCounts);});
  document.addEventListener('visibilitychange',() => {if (document.hidden) closeStream();else {void (pagePath() === '#ai' ? loadTaskList() : loadNavCounts()).catch(() => {});if (ai.modal || pageHash().startsWith('#ai/')) void loadOptions(true).then(refreshSettingsViews);if (pageHash().startsWith('#ai/')) {syncStream(ai.task);void pollTask(true);}}});
  setInterval(() => {
    if (document.hidden) return;
    if (ai.modal) updateCreateControls();
    if (pageHash().startsWith('#ai/')) void pollTask();
    else if (pageHash() === '#ai' && !ai.loading) {ai.loading = true;renderList(true,ai.sequence).finally(() => ai.loading = false);}
    // Off the AI page the list only feeds the nav count: refresh quickly while a task runs, slowly when idle.
    if (pagePath() !== '#ai') void loadNavCounts(ai.taskCounts?.active ? 5000 : 30000).catch(() => {});
  },2500);
  void (pagePath() === '#ai' ? loadTaskList() : loadNavCounts()).catch(() => {});
  if (state.detail && pageHash() === `#case/${state.detail.case_id}` && !$('.ai-case-actions')) void mountCaseActions(state.detail);
  const closeMenus = event => $$('.ai-menu[open]').forEach(menu => {if (event.key === 'Escape' || !menu.contains(event.target)) menu.open = false;});
  window.addEventListener('click',closeMenus,true);window.addEventListener('keydown',event => {if (event.key === 'Escape') closeMenus(event);});
  return {mountDraftTabs,mountCaseActions,startWriting,startFromDirection,startBlank,startFromHook,startWithMaterials,modalOpen,hasUnsavedInput,refreshAvailability,refreshDefaults,captureListState,route:routeAI,formatRound,taskDisplayTitle:contextTitle,taskManagementLabel:managementLabel,taskScriptsLoaded};
})();
