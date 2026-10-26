'use strict';

window.ContentFlow = (() => {
  // 待我处理: needs you; 待发布: finished, waiting to be posted; 全部: everything, archived included.
  const views = {attention:'待处理',running:'进行中',publish:'待发布',all:'全部'};
  const statuses = {all:'全部状态',workflow_incomplete:'链路未完结',in_progress:'制作中',needs_attention:'需处理',awaiting_review:'待核对 / 审查',approved:'已完成待发布',published:'已登记发布',archived:'已归档'};
  const kinds = {all:'所有形式',task:'协作任务',script:'待制作采用稿',video:'视频',carousel:'图文'};
  const fields = {kind:{label:'内容类型',options:kinds},status:{label:'状态',options:statuses},view:{label:'工作阶段',options:views},
    review_calendar:{label:'审查后日历',options:{all:'全部',added:'已添加到日历',not_added:'未添加到日历'}}};
  const copy = value => JSON.parse(JSON.stringify(value));
  function validFilter(value) {
    return value && ['all','any'].includes(value.match) && Array.isArray(value.conditions) && value.conditions.length <= 12 && value.conditions.every(rule =>
      rule && Object.hasOwn(fields,rule.field) && Array.isArray(rule.values) && rule.values.length > 0 && rule.values.length < Object.keys(fields[rule.field].options).length && rule.values.every(value => value !== 'all' && Object.hasOwn(fields[rule.field].options,value)));
  }
  const tags = {task:'候选',script:'采用稿',video:'视频',carousel:'图文'};
  const page = {sequence:0,product:'',filters:null,selecting:false,selected:new Set(),archiving:false,deleting:false};
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const product = () => window.WorkbenchShell?.product() || '';
  const hash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const errorText = error => error?.message || '操作暂未完成';
  const filterScope = () => JSON.stringify([page.filters.view,page.filters.kind,page.filters.status,page.filters.q,page.filters.page,page.filters.filter]);
  const currentData = () => !!page.data && page.data.product === page.product && page.product === product() && page.dataScope === filterScope() && !!$('#content-flow-list');
  // Creation has one list. Secondary records return here without another parallel tab bar.
  function tabs(active) {
    return active === 'ai' ? `<a class="back-link" href="${esc(window.WorkbenchShell?.listURL?.('content') || url('#content'))}">${icon('back')}返回内容列表</a>` : '';
  }
  function readFilters() {
    const params = new URLSearchParams(hash().split('?')[1] || '');
    let filter = null;
    try {const value = JSON.parse(params.get('filter'));if (validFilter(value)) filter = value;} catch { /* Legacy links remain usable. */ }
    return {filter,saved:params.get('saved') || '',view:filter ? 'all' : Object.hasOwn(views,params.get('view')) ? params.get('view') : 'attention',
      kind:!filter && Object.hasOwn(kinds,params.get('kind')) ? params.get('kind') : 'all',status:!filter && Object.hasOwn(statuses,params.get('status')) ? params.get('status') : 'all',q:(params.get('q') || '').slice(0,200),
      page:Math.max(1,Number.parseInt(params.get('page'),10) || 1)};
  }
  function filterHash(filters = page.filters) {
    const params = new URLSearchParams();
    if (filters.view !== 'attention') params.set('view',filters.view);
    if (filters.kind !== 'all') params.set('kind',filters.kind);
    if (filters.status && filters.status !== 'all') params.set('status',filters.status);
    if (filters.q) params.set('q',filters.q);
    if (filters.filter) params.set('filter',JSON.stringify(filters.filter));
    if (filters.saved) params.set('saved',filters.saved);
    if (filters.page > 1) params.set('page',String(filters.page));
    return url('#content' + (params.size ? '?' + params : ''),page.product);
  }
  function captureListState() {
    if (!$('#content-flow-list')) return null;
    return {product:page.product,hash:filterHash({...page.filters,q:$('#content-flow-query')?.value || ''})};
  }
  // Presets and legacy links become visible rules when the user edits them.
  function effectiveFilter(filters = page.filters) {
    if (filters.filter) return copy(filters.filter);
    return {match:'all',conditions:[['view',filters.view],['kind',filters.kind],['status',filters.status]]
      .filter(([,value]) => value && value !== 'all').map(([field,value]) => ({field,values:[value]}))};
  }
  const viewCatalogs = new Map();
  const viewStorageKey = owner => 'case-content-views-v1:' + owner.toUpperCase();
  function viewCatalog(owner = page.product) {
    if (!viewCatalogs.has(owner)) viewCatalogs.set(owner,{views:[],revision:null,ready:false,error:'',generation:0,request:null,busy:false,migrating:false});
    return viewCatalogs.get(owner);
  }
  const savedViews = () => viewCatalog().views;
  const validView = row => row && typeof row.id === 'string' && typeof row.name === 'string' && row.name.trim() && row.name.length <= 40 && typeof row.q === 'string' && row.q.length <= 200 && validFilter(row.filter);
  function acceptViews(owner,data) {
    if (data?.product !== owner || !Array.isArray(data.views) || !data.views.every(validView) || data.revision == null) throw new Error('工作台返回的视图资料不完整，请刷新重试。');
    Object.assign(viewCatalog(owner),{views:copy(data.views),revision:data.revision,ready:true,error:''});
  }
  function paintViewsFor(owner) {if (page.product === owner && product() === owner && $('#content-flow-list')) paintFilterControls();}
  async function refreshViews(owner,force = false) {
    const catalog = viewCatalog(owner);
    if (catalog.busy) return;
    if (catalog.request) return catalog.request;
    if (!force && catalog.ready && !catalog.error && Date.now() - (catalog.loadedAt || 0) < 30000) return;
    const generation = ++catalog.generation;
    let loaded = false;
    const request = (async () => {
      try {
        const data = await api('/api/content/views?' + new URLSearchParams({product:owner}));
        if (generation !== catalog.generation) return;
        acceptViews(owner,data);loaded = true;
        // Existing browser views are imported once; retain the source until the server confirms the complete import.
        let source;
        try {source = localStorage.getItem(viewStorageKey(owner));}
        catch {throw new Error('无法读取浏览器中的旧视图；工作台视图可正常使用，允许浏览器存储后点击刷新重试。');}
        if (source != null) {
          let legacy;
          try {legacy = JSON.parse(source);} catch {throw new Error('浏览器中的旧视图格式有误，原记录已保留，请检查后刷新重试。');}
          if (!Array.isArray(legacy) || legacy.length > 30 || !legacy.every(validView)) throw new Error('浏览器中的旧视图资料不完整，原记录已保留，请检查后刷新重试。');
          catalog.migrating = true;paintViewsFor(owner);
          const imported = await api('/api/content/views',{method:'POST',body:{product:owner,action:'import',views:legacy}});
          if (generation !== catalog.generation) return;
          acceptViews(owner,imported);
          // A second tab may have changed the legacy source while this request was pending.
          try {if (localStorage.getItem(viewStorageKey(owner)) === source) localStorage.removeItem(viewStorageKey(owner));}
          catch {throw new Error('旧视图已保存到工作台，但浏览器中的迁移记录未能清理；刷新后会安全重试。');}
        }
      } catch (error) {
        if (generation !== catalog.generation) return;
        if (!loaded) catalog.ready = false;
        catalog.error = loaded ? `旧视图迁移未完成：${errorText(error)} 原记录仍保留，可点击刷新重试。` : `保存的视图暂未读取：${errorText(error)} 请点击刷新重试。`;
      } finally {
        if (generation === catalog.generation) {catalog.migrating = false;catalog.loadedAt = Date.now();paintViewsFor(owner);}
      }
    })();
    catalog.request = request;
    try {await request;} finally {if (catalog.request === request) catalog.request = null;}
  }
  function closeFilterEditor() {
    const editor = $('#content-flow-filter-editor');if (editor) {editor.hidden = true;editor.innerHTML = '';}
    $('#content-flow-filter-toggle')?.setAttribute('aria-expanded','false');
    if ($('#content-flow-save-view')) $('#content-flow-save-view').disabled = !viewCatalog().ready || viewCatalog().busy || viewCatalog().migrating;
  }
  function chooseFilters(filters) {
    closeQuickFilter();clearTimeout(page.filterTimer);
    clearTimeout(page.searchTimer);page.selected.clear();page.filters = {view:'all',kind:'all',status:'all',q:'',page:1,filter:null,saved:'',...filters};
    $('#content-flow-query').value = page.filters.q;
    closeFilterEditor();paintFilterControls();void load(++page.sequence,false,true);
  }
  function paintFilterControls() {
    const root = $('#content-flow-views');if (!root) return;
    const saved = savedViews(), active = saved.find(row => row.id === page.filters.saved), filter = effectiveFilter();
    const changed = active && (JSON.stringify(active.filter) !== JSON.stringify(filter) || active.q !== page.filters.q);
    const viewMarkup = Object.entries(views).map(([key,label]) => `<button type="button" data-flow-view="${key}" aria-pressed="${!active && !page.filters.filter && page.filters.view === key}" title="预设视图：${label}">${label}<small>${page.data?.counts[key] || 0}</small></button>`).join('') +
      saved.map(row => `<button type="button" data-flow-saved-view="${esc(row.id)}" aria-pressed="${row.id === active?.id}" title="${esc(row.name)}">${esc(row.name)}${row.id === active?.id && changed ? '<small>已修改</small>' : ''}</button>`).join('');
    if (root.innerHTML !== viewMarkup) root.innerHTML = viewMarkup;
    $$('[data-flow-view]',root).forEach(button => button.onclick = () => chooseFilters({view:button.dataset.flowView}));
    $$('[data-flow-saved-view]',root).forEach(button => button.onclick = () => {const row = saved.find(row => row.id === button.dataset.flowSavedView);chooseFilters({filter:copy(row.filter),q:row.q,saved:row.id});});
    $('#content-flow-filter-toggle').textContent = `规则${filter.conditions.length ? ` · ${filter.conditions.length}` : ''}`;
    $('#content-flow-save-view').textContent = active ? '管理视图' : '保存视图';
    const catalog = viewCatalog();
    $('#content-flow-save-view').disabled = !catalog.ready || catalog.busy || catalog.migrating || !$('#content-flow-filter-editor').hidden;
    const viewError = $('#content-flow-views-error');
    if (viewError) {viewError.hidden = !catalog.error;viewError.textContent = catalog.error;}
    const summary = $('#content-flow-filter-summary');
    const summaryMarkup = (filter.conditions.length ? `<button type="button" class="flow-filter-match" title="切换为满足${filter.match === 'all' ? '任一' : '所有'}条件">${filter.match === 'all' ? '所有条件' : '任一条件'} ⇄</button>` : '') +
      filter.conditions.map((rule,index) => `<span class="flow-filter-chip-wrap"><button type="button" class="flow-filter-chip" data-quick-condition="${index}" aria-haspopup="dialog" title="修改${esc(fields[rule.field].label)}">${esc(fields[rule.field].label)}：${rule.values.map(value => esc(fields[rule.field].options[value])).join('、')}</button><button type="button" class="flow-filter-chip-remove" data-quick-remove="${index}" aria-label="移除${esc(fields[rule.field].label)}条件">×</button></span>`).join('') +
      `<button type="button" class="flow-filter-add-quick" aria-haspopup="dialog" ${filter.conditions.length >= 12 ? 'disabled' : ''}>＋筛选</button>` +
      (filter.conditions.length || page.filters.q ? '<button type="button" class="subtle" data-filter-clear>重置</button>' : '') +
      `<span class="flow-filter-meta">${active ? esc(active.name) + (changed ? ' · 已修改' : '') : page.filters.filter ? '临时筛选' : '预设视图'}</span>`;
    if (summary.innerHTML !== summaryMarkup) summary.innerHTML = summaryMarkup;
    $$('.flow-filter-chip',summary).forEach(button => button.onclick = () => openQuickFilter(button,Number(button.dataset.quickCondition)));
    $('.flow-filter-add-quick',summary).onclick = event => openQuickFilter(event.currentTarget);
    $$('[data-quick-remove]',summary).forEach(button => button.onclick = () => {closeQuickFilter();const next = effectiveFilter();next.conditions.splice(Number(button.dataset.quickRemove),1);applyQuickFilter(next);$('.flow-filter-add-quick')?.focus();});
    const matchButton = $('.flow-filter-match',summary);if (matchButton) matchButton.onclick = () => {closeQuickFilter();const next = effectiveFilter();next.match = next.match === 'all' ? 'any' : 'all';applyQuickFilter(next);$('.flow-filter-match')?.focus();};
    const clearButton = $('[data-filter-clear]',summary);if (clearButton) clearButton.onclick = () => chooseFilters({view:'all'});
  }
  function applyQuickFilter(filter) {
    clearTimeout(page.searchTimer);clearTimeout(page.filterTimer);clearTimeout(page.pollTimer);
    page.filters = {...page.filters,filter:copy(filter),view:'all',kind:'all',status:'all',page:1};
    page.selected.clear();page.sequence++;
    history.replaceState(null,'',filterHash());paintFilterControls();
    $('#content-flow-result-count').textContent = '正在筛选…';
    page.filterTimer = setTimeout(() => void load(++page.sequence,false,true),150);
  }
  function closeQuickFilter(restoreFocus = false) {
    const quick = page.quick;if (!quick) return;
    page.quick = null;quick.cleanup();quick.node.remove();
    if (restoreFocus) (quick.anchor?.isConnected ? quick.anchor : $('.flow-filter-add-quick'))?.focus({preventScroll:true});
  }
  function openQuickFilter(anchor,index = -1) {
    closeQuickFilter();closeFilterEditor();
    const filter = effectiveFilter(), node = document.createElement('div');
    const quick = {node,anchor,index,filter,rule:index >= 0 ? copy(filter.conditions[index]) : null};page.quick = quick;
    node.className = 'flow-quick-popover';node.setAttribute('role','dialog');node.setAttribute('aria-label','快捷筛选');
    document.body.append(node);
    function position() {
      if (!node.isConnected) return;
      const target = quick.anchor?.isConnected ? quick.anchor : $('.flow-filter-add-quick');
      if (!target) {closeQuickFilter();return;}
      const rect = target.getBoundingClientRect(), width = node.offsetWidth, height = node.offsetHeight;
      const viewport = window.visualViewport, bottom = (viewport?.offsetTop || 0) + (viewport?.height || innerHeight);
      node.style.left = Math.max(12,Math.min(rect.left,innerWidth - width - 12)) + 'px';
      node.style.top = Math.max(12,rect.bottom + height + 10 <= bottom ? rect.bottom + 8 : Math.min(rect.top - height - 8,bottom - height - 12)) + 'px';
    }
    function render() {
      const field = quick.rule?.field;
      node.innerHTML = `<div class="flow-quick-heading">${field ? '<button type="button" class="icon-button" data-quick-back aria-label="返回筛选字段">‹</button>' : ''}<strong>${field ? esc(fields[field].label) : '添加筛选'}</strong><button type="button" class="icon-button" data-quick-close aria-label="关闭筛选">${icon('close')}</button></div><input type="search" class="flow-quick-search" aria-label="搜索筛选选项" placeholder="${field ? '搜索选项…' : '筛选方式…'}"><div class="flow-quick-options"></div><div class="flow-quick-footer"><small>${field === 'review_calendar' ? '仅匹配当前版本已完成剪辑审查的视频' : field ? '可多选 · 选中即筛选' : '选择字段，快速添加条件'}</small><button type="button" class="subtle" data-quick-done>完成</button></div>`;
      const list = $('.flow-quick-options',node), search = $('.flow-quick-search',node);
      function options() {
        const query = search.value.trim().toLocaleLowerCase();
        const entries = Object.entries(field ? fields[field].options : fields).filter(([key,value]) => key !== 'all' && (field ? value : value.label).toLocaleLowerCase().includes(query));
        list.innerHTML = entries.map(([key,value]) => field ? `<label class="flow-quick-option"><input type="checkbox" data-quick-value="${esc(key)}" ${quick.rule.values.includes(key) ? 'checked' : ''}><span>${esc(value)}</span></label>` : `<button type="button" data-quick-field="${key}"><span class="flow-quick-field-icon" aria-hidden="true">${{kind:'▧',status:'◉',view:'☷',review_calendar:'▦'}[key]}</span><span>${esc(value.label)}</span><small>${Object.keys(value.options).length - 1} 项</small></button>`).join('') || '<p class="flow-quick-empty">没有匹配的选项</p>';
        $$('[data-quick-field]',list).forEach(button => button.onclick = () => {
          // Switching fields starts a new condition; existing chips retain their explicit rules.
          const selected = button.dataset.quickField;
          if (quick.rule?.field !== selected) {quick.index = -1;quick.rule = {field:selected,values:[]};}
          render();
        });
        $$('[data-quick-value]',list).forEach(input => input.onchange = () => {
          const value = input.dataset.quickValue;
          quick.rule.values = input.checked ? [...quick.rule.values,value] : quick.rule.values.filter(entry => entry !== value);
          if (quick.rule.values.length) {
            if (quick.index < 0 && quick.filter.conditions.length >= 12) {input.checked = false;quick.rule.values = quick.rule.values.filter(entry => entry !== value);toast('最多添加 12 条条件');return;}
            if (quick.index < 0) quick.index = quick.filter.conditions.length;
            quick.filter.conditions[quick.index] = copy(quick.rule);
          } else if (quick.index >= 0) {quick.filter.conditions.splice(quick.index,1);quick.index = -1;}
          applyQuickFilter(quick.filter);
          quick.anchor = quick.index >= 0 ? $(`[data-quick-condition="${quick.index}"]`) : $('.flow-filter-add-quick');position();
        });position();
      }
      search.oninput = options;options();
      $('[data-quick-close]',node).onclick = () => closeQuickFilter(true);
      $('[data-quick-done]',node).onclick = () => closeQuickFilter(true);
      $('[data-quick-back]',node)?.addEventListener('click',() => {quick.rule = null;quick.index = -1;render();});
      search.focus({preventScroll:true});position();
    }
    const outside = event => {if (!node.contains(event.target) && !quick.anchor?.contains(event.target)) closeQuickFilter();};
    const keydown = event => {
      if (event.key === 'Escape') {event.preventDefault();event.stopPropagation();closeQuickFilter(true);return;}
      if (event.key === 'Tab' && node.contains(event.target)) {
        const focusable = $$('button:not(:disabled),input:not(:disabled)',node), at = focusable.indexOf(document.activeElement);
        if (event.shiftKey && at === 0 || !event.shiftKey && at === focusable.length - 1) {event.preventDefault();focusable[event.shiftKey ? focusable.length - 1 : 0]?.focus();}
      }
      if (event.key === 'ArrowDown' && document.activeElement === $('.flow-quick-search',node)) {event.preventDefault();$('[data-quick-field],[data-quick-value]',node)?.focus();}
    };
    document.addEventListener('pointerdown',outside);node.addEventListener('keydown',keydown);
    window.addEventListener('resize',position);document.addEventListener('scroll',position,true);
    quick.cleanup = () => {document.removeEventListener('pointerdown',outside);window.removeEventListener('resize',position);document.removeEventListener('scroll',position,true);};
    render();
  }
  function openFilterEditor() {
    closeQuickFilter();
    const editor = $('#content-flow-filter-editor');if (!editor) return;
    const draft = effectiveFilter();editor.hidden = false;
    $('#content-flow-filter-toggle').setAttribute('aria-expanded','true');
    $('#content-flow-save-view').disabled = true;
    editor.innerHTML = `<div class="flow-filter-heading"><label>满足以下<select id="content-filter-match" aria-label="条件匹配方式"><option value="all">所有</option><option value="any">任一</option></select>条件</label><span>每条条件可多选，选中任一值即可匹配</span></div><div data-filter-rows></div><div class="flow-filter-footer"><button type="button" class="subtle" data-filter-add>＋添加条件</button><span data-filter-hint role="status"></span><button type="button" class="secondary" data-filter-cancel>取消</button><button type="button" class="primary" data-filter-apply>应用筛选</button></div>`;
    $('#content-filter-match').value = draft.match;
    $('#content-filter-match').onchange = event => {draft.match = event.target.value;};
    const update = () => {
      $('[data-filter-apply]',editor).disabled = !validFilter(draft);
      $('[data-filter-add]',editor).disabled = draft.conditions.length >= 12;
      $('[data-filter-hint]',editor).textContent = !draft.conditions.length ? '未添加条件，将显示全部内容' : !validFilter(draft) ? '请为每条条件至少选择一个值' : draft.conditions.some(rule => rule.field === 'review_calendar') ? '审查后日历：仅包含当前版本已完成剪辑审查的视频' : '';
    };
    function renderRows() {
      const rows = $('[data-filter-rows]',editor);
      rows.innerHTML = draft.conditions.map((rule,index) => `<div class="flow-filter-row" data-filter-row="${index}"><select data-filter-field aria-label="条件 ${index + 1} 字段">${Object.entries(fields).map(([key,field]) => `<option value="${key}" ${key === rule.field ? 'selected' : ''}>${field.label}</option>`).join('')}</select><span class="flow-filter-operator">属于</span><details class="flow-filter-values"><summary data-filter-values aria-label="条件 ${index + 1} 的值"></summary><div class="flow-filter-options">${Object.entries(fields[rule.field].options).filter(([value]) => value !== 'all').map(([value,label]) => `<label><input type="checkbox" data-filter-value="${esc(value)}" ${rule.values.includes(value) ? 'checked' : ''}><span>${esc(label)}</span></label>`).join('')}</div></details><button type="button" class="icon-button" data-filter-remove aria-label="删除条件 ${index + 1}">${icon('close')}</button></div>`).join('');
      $$('[data-filter-row]',rows).forEach((row,index) => {
        const rule = draft.conditions[index];
        const label = () => {$('[data-filter-values]',row).textContent = rule.values.length ? rule.values.map(value => fields[rule.field].options[value]).join('、') : '选择值';};label();
        $('[data-filter-field]',row).onchange = event => {rule.field = event.target.value;rule.values = [];renderRows();};
        $('[data-filter-remove]',row).onclick = () => {draft.conditions.splice(index,1);renderRows();$('[data-filter-add]',editor).focus();};
        $$('[data-filter-value]',row).forEach(input => input.onchange = () => {rule.values = $$('[data-filter-value]:checked',row).map(input => input.dataset.filterValue);label();update();});
      });update();
    }
    $('[data-filter-add]',editor).onclick = () => {if (draft.conditions.length < 12) {draft.conditions.push({field:'kind',values:[]});renderRows();const row = $('[data-filter-rows]',editor).lastElementChild;$('details',row).open = true;$('[data-filter-values]',row).focus();}};
    $('[data-filter-cancel]',editor).onclick = () => {closeFilterEditor();$('#content-flow-filter-toggle').focus();};
    $('[data-filter-apply]',editor).onclick = () => {if (validFilter(draft)) chooseFilters({filter:copy(draft),q:page.filters.q,saved:page.filters.saved});};
    renderRows();
  }
  function saveViewDialog() {
    const owner = page.product, catalog = viewCatalog(owner), existing = savedViews().find(row => row.id === page.filters.saved);
    if (!catalog.ready || catalog.busy || catalog.migrating) {toast('视图资料读取完成后再保存；读取失败时可点击刷新重试。',true);return;}
    const snapshot = {filter:effectiveFilter(),q:page.filters.q};
    // Pin the dialog's revision: background polling must never make an old draft overwrite a newer edit.
    const revision = catalog.revision, newId = crypto.randomUUID();
    let saving = false, conflicted = false;
    const dialog = document.createElement('dialog');dialog.className = 'script-editor-dialog production-dialog flow-view-dialog';
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>${existing ? '管理视图' : '保存视图'}</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div><div class="script-editor-body"><label class="field"><span>视图名称</span><input name="name" maxlength="40" required placeholder="例如：待审视频" value="${esc(existing?.name || '')}"></label><p class="context-note">保存当前筛选条件和搜索词。视图保存在工作台，供${esc(page.product || '全部产品')}使用；换浏览器或设备访问同一工作台也能继续使用。</p><p class="ai-error" role="alert" data-view-error hidden></p></div><div class="dialog-footer">${existing ? '<button type="button" class="subtle flow-delete-archived" data-view-delete>删除视图</button>' : '<span></span>'}<div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="${existing ? 'secondary' : 'primary'}" data-view-save>${existing ? '另存为新视图' : '保存视图'}</button>${existing ? '<button type="button" class="primary" data-view-update>保存修改</button>' : ''}</div></div></form>`;
    document.body.append(dialog);
    const close = () => {dialog.close();dialog.remove();};
    $$('[data-close]',dialog).forEach(button => button.onclick = close);
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    const fail = message => {const node = $('[data-view-error]',dialog);node.hidden = false;node.textContent = message;};
    const stillHere = () => dialog.isConnected && owner === page.product && owner === product() && !!$('#content-flow-query');
    const controls = () => {
      $$('[data-view-save],[data-view-update],[data-view-delete]',dialog).forEach(button => {
        button.disabled = saving || !catalog.ready || catalog.migrating || (conflicted && !button.hasAttribute('data-view-save'));
      });
      $('[name=name]',dialog).disabled = saving;
    };
    async function persist(action,record,update = false) {
      if (!stillHere()) {close();return;}
      if (saving || catalog.busy || !catalog.ready || catalog.migrating) return;
      saving = true;catalog.busy = true;catalog.generation++;catalog.request = null;controls();paintViewsFor(owner);
      $('[data-view-error]',dialog).hidden = true;
      try {
        const result = await api('/api/content/views',{method:'POST',body:{product:owner,action,
          revision:conflicted && action === 'save' && !update ? catalog.revision : revision,...(action === 'save' ? {view:record} : {id:existing.id})}});
        acceptViews(owner,result);
        if (!stillHere()) {paintViewsFor(owner);return;}
        close();
        if (action === 'delete') {page.filters.saved = '';history.replaceState(null,'',filterHash());paintFilterControls();toast('视图已删除，当前筛选仍保留');}
        else {const stored = catalog.views.find(row => row.id === record.id) || record;chooseFilters({filter:copy(stored.filter),q:stored.q,saved:stored.id});toast(update ? '视图已更新' : '视图已保存到工作台');}
      } catch (error) {
        if (error?.status === 409 && !window.WorkbenchMaintenance?.isError?.(error)) {
          conflicted = true;catalog.busy = false;
          await refreshViews(owner,true);
          if (stillHere()) fail(`视图已在其他浏览器或设备发生变化。你的输入已保留，请关闭后重新打开，核对最新视图后再修改${existing ? '，或另存为新视图' : ''}。${catalog.error ? ' ' + catalog.error : ''}`);
        } else if (stillHere()) fail(`视图${action === 'delete' ? '删除' : '保存'}未完成：${errorText(error)} 可重试，当前输入已保留。`);
      } finally {
        saving = false;catalog.busy = false;controls();paintViewsFor(owner);
      }
    }
    function save(update) {
      if (owner !== page.product || owner !== product() || !$('#content-flow-query')) {close();return;}
      if (saving || (update && conflicted)) return;
      const name = $('[name=name]',dialog).value.trim(), rows = savedViews();
      if (!name) {fail('请填写视图名称。');return;}
      if (rows.some(row => row.id !== (update ? existing.id : newId) && row.name.toLocaleLowerCase() === name.toLocaleLowerCase()) || Object.values(views).includes(name)) {fail('这个名称已被使用，请换一个名称。');return;}
      if (!update && !rows.some(row => row.id === newId) && rows.length >= 30) {fail('最多保存 30 个视图，请先删除不再使用的视图。');return;}
      const record = {id:update ? existing.id : newId,name,...copy(snapshot)};
      void persist('save',record,update);
    }
    $('form',dialog).onsubmit = event => {event.preventDefault();save(false);};
    $('[data-view-update]',dialog)?.addEventListener('click',() => save(true));
    $('[data-view-delete]',dialog)?.addEventListener('click',() => {if (!conflicted) void persist('delete');});
    dialog.showModal();$('[name=name]',dialog).focus();
  }
  // Production records show their current workflow; candidates keep compact rows.
  const dots = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg>';
  function when(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return '';
    return date.toDateString() === new Date().toDateString() ? date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}) : `${date.getMonth() + 1}/${date.getDate()}`;
  }
  const tone = item => item.history || item.archived ? 'muted' : item.busy ? 'active' : item.needs_attention ? 'warning' : 'muted';
  // An order may carry several videos; each is its own link, numbered when there is more than one.
  function tiktokLinks(flow) {
    const posts = (flow.posts?.length ? flow.posts : flow.post_url ? [{url:flow.post_url}] : []).filter(post => /^https?:\/\//.test(post.url || ''));
    return posts.map((post,index) => `<a class="flow-tiktok" href="${esc(post.url)}" target="_blank" rel="noopener noreferrer" title="在 TikTok 打开已发布的帖子">TikTok${posts.length > 1 ? ` ${index + 1}` : ''} ↗</a>`).join('');
  }
  function workflowMarkup(flow,product,{linked = false} = {}) {
    const complete = flow.complete === true, label = flow.kind === 'carousel' ? '图文链' : '视频链';
    const id = String(flow.id || '').replace(/^(?:video|carousel):/,'');
    const heading = linked && flow.hash ? `<a class="flow-workflow-label" href="${esc(url(flow.hash,product))}" title="${esc(flow.title || '关联图文')}">${label}${flow.title ? ` · ${esc(flow.title)}` : ''}${flow.archived ? '（已归档）' : ''}</a>` : `<span class="flow-workflow-label">${label}</span>`;
    const chips = (flow.steps || []).map(step => {
      const status = step.done ? '已完成' : step.active ? '当前待完成' : '未完成';
      const attrs = `class="flow-chain-step" data-step-key="${esc(step.key)}" data-done="${step.done === true}" data-active="${step.active === true}" title="${esc(step.label)}：${status}" aria-label="${esc(step.label)}：${status}"`;
      return step.hash ? `<a ${attrs} href="${esc(url(step.hash,product))}"><i aria-hidden="true"></i>${esc(step.label)}</a>` : `<span ${attrs}><i aria-hidden="true"></i>${esc(step.label)}</span>`;
    });
    // 日历 comes before 发布 (upload to the calendar, then post) and never gates 已完结.
    const calendar = typeof flow.calendar_uploaded === 'boolean' ? `<span class="flow-chain-step" data-step-key="calendar" data-done="${flow.calendar_uploaded}" data-active="false" title="发布日历：${flow.calendar_uploaded ? '已上传' : '未上传'}" aria-label="发布日历：${flow.calendar_uploaded ? '已上传' : '未上传'}"><i aria-hidden="true"></i>日历</span>` : '';
    const at = chips.findIndex((_,index) => flow.steps[index].key === 'publish');
    const steps = (calendar ? chips.toSpliced(at < 0 ? chips.length : at,0,calendar) : chips).join('');
    return `<div class="flow-workflow" data-workflow-kind="${esc(flow.kind)}" data-workflow-id="${esc(id)}" data-linked="${linked}" data-complete="${complete}">${heading}<div class="flow-chain">${steps}</div><span class="flow-chain-result" data-complete="${complete}">${esc(flow.label || `${label}${complete ? '已完结' : '未完结'}`)}</span>${tiktokLinks(flow)}</div>`;
  }
  function productionRow(item,{target,title,select,menu}) {
    const own = {id:item.production_ids[0],kind:item.kind,steps:item.workflow_steps || [],complete:item.workflow_complete === true,label:item.workflow_label,calendar_uploaded:item.calendar_uploaded,post_url:item.post_url,posts:item.posts};
    const related = item.related_workflows || [];
    const workflows = workflowMarkup(own,item.product) + related.map(flow => workflowMarkup(flow,item.product,{linked:true})).join('') +
      (item.kind === 'video' && !related.length ? '<div class="flow-workflow" data-workflow-kind="carousel" data-linked="true"><span class="flow-workflow-label">图文链</span><span class="flow-chain-result">尚未关联图文</span></div>' : '');
    const meta = [item.product,item.direction_id,item.timeline_name,item.unit_count ? `${item.unit_count} ${item.unit_label || (item.kind === 'video' ? '个意群' : '张图片')}` : '',readableDate(item.updated_at)].filter(Boolean).map(esc).join(' · ');
    const note = item.summary_note || item.progress_text || item.stage_label || '查看当前制作进度';
    const success = own.complete || own.steps.some(step => ['edit','confirm'].includes(step.key) && step.done === true);
    return `<article class="flow-row is-production${item.history || item.archived ? ' is-finished' : ''}" data-content-id="${esc(item.id)}">${select}<div class="flow-record-main"><div class="flow-record-title"><span class="flow-kind">${esc(tags[item.kind])}</span><a class="flow-title" href="${target}" title="${title}">${title}</a></div><div class="flow-record-meta">${meta}</div><p class="flow-record-note${item.progress_text === note ? ' flow-progress' : ''}">${esc(note)}</p>${item.progress_text && item.progress_text !== note ? `<p class="flow-progress">${esc(item.progress_text)}</p>` : ''}<div class="flow-workflows">${workflows}</div></div><div class="flow-record-actions"><span class="flow-stage is-${item.archived ? 'muted' : success ? 'success' : tone(item)}">${esc(item.archived ? '已归档' : item.stage_label || '待处理')}</span><a class="flow-next" href="${target}">${esc(item.next_label || '查看详情')}${icon('arrow')}</a></div>${menu}</article>`;
  }
  function row(item) {
    const links = item.links || [], view = page.filters.view, title = esc(item.title || '未命名内容');
    const target = esc(url(item.next_hash || links[0]?.hash || '#ai',item.product));
    const select = page.selecting ? `<input class="flow-select" type="checkbox" data-flow-select="${esc(item.id)}" aria-label="选择 ${title}" ${item.busy ? 'disabled' : ''} ${page.selected.has(item.id) ? 'checked' : ''}>` : '';
    // 待发布 rows all share one stage and one next step, so that view names neither.
    const stage = view === 'publish' ? '' : `<span class="flow-stage is-${tone(item)}">${esc(item.archived ? '已归档' : item.stage_label || '待处理')}</span>`;
    const next = ['attention','running'].includes(view) ? `<a class="flow-next" href="${target}">${esc(item.next_label || '继续处理')}${icon('arrow')}</a>` : '';
    const archive = `<button type="button" role="menuitem" data-archive="${esc(item.id)}" ${item.busy ? 'disabled title="任务结束后可整理"' : ''}>${item.archived ? '恢复' : '归档'}</button>`;
    const remove = ['video','carousel'].includes(item.kind) ? `<button type="button" role="menuitem" data-delete-order="${esc(item.id)}" ${item.busy ? 'disabled' : ''}>永久删除</button>` : '';
    const menu = `<details class="ai-menu flow-menu"><summary class="icon-button" aria-label="更多操作：${title}">${dots}</summary><div role="menu">${links.map(link => `<a role="menuitem" href="${esc(url(link.hash,item.product))}">${esc(link.label)}</a>`).join('')}${archive}${remove}</div></details>`;
    if (['video','carousel'].includes(item.kind)) return productionRow(item,{target,title,select,menu});
    const started = item.task_started_label ? `<small class="flow-task-started">${esc(item.task_started_label)}</small>` : '';
    return `<article class="flow-row${item.history || item.archived ? ' is-finished' : ''}" data-content-id="${esc(item.id)}">${select}<span class="flow-kind">${esc(item.kind_label || tags[item.kind] || item.kind)}</span><span class="flow-candidate-main"><a class="flow-title" href="${target}" title="${title}">${title}</a>${started}${item.progress_text ? `<small class="flow-progress">${esc(item.progress_text)}</small>` : ''}</span><span class="flow-direction">${esc(item.direction_id || '')}</span>${stage}${next}<time datetime="${esc(item.updated_at || '')}">${esc(when(item.updated_at))}</time>${menu}</article>`;
  }
  async function route(keepScroll = false) {
    const seq = ++page.sequence;
    const same = page.product === product() && $('#content-flow-list');
    const mounted = keepScroll && same ? {...page.filters,q:$('#content-flow-query').value} : null;
    if (page.product !== product()) {page.data = null;page.resultNode = null;page.resultSignature = null;page.selected.clear();page.selecting = false;}
    page.product = product();page.filters = mounted || readFilters();
    if (!mounted) {closeQuickFilter();clearTimeout(page.filterTimer);page.selecting = false;page.selected.clear();}
    $('#breadcrumb').textContent = '内容创作';
    if (!same || !keepScroll) {
      $('#content').innerHTML = `<div class="page-heading"><div><h1>内容创作</h1><p>从候选到发布，查看每条内容走到哪一步、下一步做什么。</p></div><div class="flow-heading-actions"><button type="button" class="secondary" id="content-flow-refresh">${icon('refresh')}刷新</button><details class="ai-menu flow-main-menu"><summary class="secondary" aria-label="内容更多操作">更多</summary><div role="menu"><a role="menuitem" href="${esc(url('#ai'))}">协作记录</a><button type="button" role="menuitem" id="content-production-settings">制作设置</button><button type="button" role="menuitem" id="content-flow-batch-open">批量管理</button></div></details></div></div><div class="flow-views" id="content-flow-views" role="group" aria-label="内容视图"></div><div class="flow-filters"><label class="search">${icon('search')}<input id="content-flow-query" type="search" maxlength="200" placeholder="搜索内容、方向或编号" aria-label="搜索内容"></label><button type="button" class="secondary" id="content-flow-filter-toggle" aria-expanded="false" aria-controls="content-flow-filter-editor">筛选</button><button type="button" class="secondary" id="content-flow-save-view">保存视图</button><button type="button" class="secondary" id="content-flow-select" hidden>退出批量管理</button><button type="button" class="secondary flow-delete-archived" id="content-flow-delete-archived" hidden>删除已归档</button></div><div id="content-flow-filter-summary" class="flow-filter-summary"></div><section id="content-flow-filter-editor" class="flow-filter-editor" aria-label="自定义筛选" hidden></section><p id="content-flow-result-count" class="context-note" aria-live="polite"></p><div class="flow-batch" id="content-flow-batch" hidden></div><p id="content-flow-status" class="context-note" role="status"></p><p id="content-flow-error" class="ai-error" role="alert" hidden></p><div id="content-flow-list" class="flow-list">${skeletonMarkup(2)}</div><nav id="content-flow-pages" class="flow-pages" aria-label="内容分页"></nav>`;
      const viewError = document.createElement('p');viewError.id = 'content-flow-views-error';viewError.className = 'ai-error';viewError.setAttribute('role','alert');viewError.hidden = true;
      $('#content-flow-filter-summary').after(viewError);
      $('#content-flow-query').value = page.filters.q;
      $('#content-flow-query').oninput = () => {
        page.filters.q = $('#content-flow-query').value;page.filters.page = 1;page.selected.clear();page.sequence++;paintFilterControls();
        clearTimeout(page.searchTimer);page.searchTimer = setTimeout(() => void load(++page.sequence,false,true),250);
      };
      $('#content-flow-refresh').onclick = () => {clearTimeout(page.filterTimer);clearTimeout(page.searchTimer);void load(++page.sequence,true);};
      $('#content-flow-filter-toggle').onclick = () => {if ($('#content-flow-filter-editor').hidden) openFilterEditor();else closeFilterEditor();};
      $('#content-flow-save-view').onclick = saveViewDialog;
      paintFilterControls();
      // Coming back to the same list: show the rows read last time at once; load() below confirms them
      // and repaints only if something changed.
      if (currentData() && Date.now() - (page.dataAt || 0) < 300000) {
        const node = $('#content-flow-list');
        page.resultNode = node;page.resultSignature = JSON.stringify([page.filters,page.data]);
        $('#content-flow-result-count').textContent = `共 ${page.data.total} 条内容`;
        paint();paintPages(page.data);
      }
      $('#content-production-settings').onclick = event => {event.target.closest('details').open = false;void window.CaseProduction?.openSettings?.();};
      $('#content-flow-batch-open').onclick = event => {event.target.closest('details').open = false;if (!currentData()) {toast('列表读取完成后可批量管理。');return;}page.selecting = true;page.selected.clear();paint();};
      $('#content-flow-select').onclick = () => {page.selecting = !page.selecting;page.selected.clear();paint();};
      $('#content-flow-delete-archived').onclick = () => void deleteArchived();
    }
    await load(seq,true);
  }
  function paintBatch() {
    const bar = $('#content-flow-batch'), toggle = $('#content-flow-select'), remove = $('#content-flow-delete-archived');
    if (!bar || !toggle) return;
    if (remove) {remove.hidden = !page.data?.archived_deletable;remove.disabled = page.deleting;remove.textContent = `删除已归档 ${page.data?.archived_deletable || 0} 项`;}
    toggle.hidden = !page.selecting;toggle.disabled = page.archiving || page.deleting;
    bar.hidden = !page.selecting;
    if (!page.selecting) return;
    const chosen = (page.data?.items || []).filter(item => page.selected.has(item.id));
    const live = chosen.filter(item => !item.archived && !item.busy), archived = chosen.filter(item => item.archived && !item.busy);
    const orders = chosen.filter(item => ['video','carousel'].includes(item.kind) && !item.busy);
    const busy = page.archiving || page.deleting;
    bar.innerHTML = `<span role="status">已选 ${chosen.length} 项</span><button type="button" class="subtle" data-flow-select-all ${busy ? 'disabled' : ''}>全选本页</button><button type="button" class="secondary compact" data-flow-archive-selected ${live.length && !busy ? '' : 'disabled'}>归档 ${live.length || ''}</button><button type="button" class="secondary compact" data-flow-restore-selected ${archived.length && !busy ? '' : 'disabled'}>恢复 ${archived.length || ''}</button><button type="button" class="secondary compact flow-delete-archived" data-flow-delete-selected ${orders.length && !busy ? '' : 'disabled'}>永久删除制作记录 ${orders.length || ''}</button>${chosen.length > orders.length && orders.length ? '<small>只删除所选视频、图文的制作记录，候选和采用稿保留。</small>' : ''}`;
    $('[data-flow-select-all]',bar).onclick = () => {page.data.items.filter(item => !item.busy).forEach(item => page.selected.add(item.id));paint();};
    $('[data-flow-archive-selected]',bar).onclick = () => void archive(live.map(item => item.id),true);
    $('[data-flow-restore-selected]',bar).onclick = () => void archive(archived.map(item => item.id),false);
    $('[data-flow-delete-selected]',bar).onclick = () => void deleteOrders(orders);
  }
  async function deleteOrders(items) {
    if (page.archiving || page.deleting || !items.length || !currentData()) return;
    if (items.some(item => !page.data.items.some(row => row.id === item.id))) return;
    await window.CaseProduction?.deleteContentOrders?.(items.map(item => ({id:item.production_ids[0],kind:item.kind})),{
      onComplete:async result => {
        const failed = new Set((result.results || []).filter(entry => !entry.ok).map(entry => entry.id));
        page.selected = new Set(page.data.items.filter(item => item.production_ids.some(id => failed.has(id))).map(item => item.id));
        page.selecting = page.selected.size > 0;paint();await load(++page.sequence);
      }
    });
  }
  function paint() {
    const node = $('#content-flow-list'), data = page.data;
    if (!node || !data || !currentData()) return;
    const opened = new Set($$('.flow-menu[open]',node).map(menu => menu.closest('[data-content-id]')?.dataset.contentId));
    node.dataset.view = page.filters.view;
    node.classList.toggle('is-selecting',page.selecting);
    node.innerHTML = data.items.length ? data.items.map(row).join('') : `<div class="empty"><h3>${data.counts.all ? '当前视图没有内容' : '还没有创作记录'}</h3><p>${data.counts.all ? '切换视图或调整搜索，查看其他内容。' : '开始写稿后，候选、脚本和制作记录会出现在这里。'}</p>${data.counts.all ? '<button type="button" class="secondary" data-flow-all>查看全部</button>' : '<button type="button" class="primary" data-flow-start>开始写稿</button>'}</div>`;
    $$('.flow-menu',node).forEach(menu => {if (opened.has(menu.closest('[data-content-id]')?.dataset.contentId)) menu.open = true;});
    $('[data-flow-all]',node)?.addEventListener('click',() => chooseFilters({view:'all'}));
    $('[data-flow-start]',node)?.addEventListener('click',() => void window.CaseAI?.startBlank());
    $$('[data-archive]',node).forEach(button => button.onclick = () => {const item = data.items.find(entry => entry.id === button.dataset.archive);if (item) void archive([item.id],!item.archived);});
    $$('[data-delete-order]',node).forEach(button => button.onclick = () => {const item = data.items.find(entry => entry.id === button.dataset.deleteOrder);if (item) void deleteOrders([item]);});
    $$('[data-flow-select]',node).forEach(box => box.onchange = () => {if (box.checked) page.selected.add(box.dataset.flowSelect);else page.selected.delete(box.dataset.flowSelect);paintBatch();});
    paintBatch();
  }
  function status(text) {const node = $('#content-flow-status');if (node) {node.textContent = text;node.hidden = !text;}}
  // reuse: the data did not change (filter, search, page changes within the open list), so the server may answer from its recent snapshot.
  async function load(seq,refreshSaved = false,reuse = false) {
    const node = $('#content-flow-list');if (!node) return;
    node.setAttribute('aria-busy','true');
    const chosenProduct = page.product;
    const {filter,saved,...query} = page.filters;
    const params = new URLSearchParams({product:chosenProduct,...query,page_size:'30'});
    if (filter) params.set('filter',JSON.stringify(filter));
    if (reuse) params.set('reuse','1');
    status(!page.data || !page.resultNode?.isConnected ? '正在读取内容进程…' : '');
    $('#content-flow-error').hidden = true;
    try {
      // A slow saved-view read must not hold up content results. Filter changes reuse recent view metadata.
      void refreshViews(chosenProduct,refreshSaved);
      const data = await api('/api/content?' + params);
      if (seq !== page.sequence || !node.isConnected || chosenProduct !== product()) return;
      node.setAttribute('aria-busy','false');
      status('');
      const signature = JSON.stringify([page.filters,data]);
      if (node === page.resultNode && page.resultSignature === signature) {schedulePoll(node,data);return;}
      const focusedId = document.activeElement?.id;
      page.resultNode = node;page.resultSignature = signature;
      page.data = data;page.dataAt = Date.now();page.filters.page = data.page;
      page.dataScope = filterScope();
      // Selections only survive while their rows are still listed.
      page.selected = new Set([...page.selected].filter(id => data.items.some(item => item.id === id)));
      history.replaceState(null,'',filterHash());
      paintFilterControls();
      $('#content-flow-result-count').textContent = `共 ${data.total} 条内容`;
      paint();
      if (focusedId) document.getElementById(focusedId)?.focus({preventScroll:true});
      paintPages(data);
      schedulePoll(node,data);
    } catch (error) {
      if (seq !== page.sequence || !node.isConnected) return;
      node.setAttribute('aria-busy','false');
      if (!currentData()) $('#content-flow-result-count').textContent = '筛选未更新，请重试';
      status('');
      $('#content-flow-error').textContent = errorText(error);$('#content-flow-error').hidden = false;
      if (!page.data || page.data.product !== chosenProduct) node.innerHTML = '<div class="empty"><h3>内容进程暂未读取</h3><p>点击刷新重试。</p></div>';
    }
  }
  function paintPages(data) {
    $('#content-flow-pages').innerHTML = data.pages > 1 ? `<button type="button" class="secondary" data-flow-page="${data.page - 1}" ${data.page === 1 ? 'disabled' : ''}>上一页</button><span>第 ${data.page} / ${data.pages} 页</span><button type="button" class="secondary" data-flow-page="${data.page + 1}" ${data.page === data.pages ? 'disabled' : ''}>下一页</button>` : '';
    $$('[data-flow-page]').forEach(button => button.onclick = () => {page.filters.page = Number(button.dataset.flowPage);void load(++page.sequence,false,true);});
  }
  function schedulePoll(node,data) {
    clearTimeout(page.pollTimer);
    page.pollTimer = setTimeout(() => {if (node.isConnected && !page.archiving && !page.deleting && !document.hidden) void load(++page.sequence,true);},data.items.some(item => item.busy) ? 5000 : 30000);
  }
  function leaveList() {
    clearTimeout(page.pollTimer);clearTimeout(page.searchTimer);clearTimeout(page.filterTimer);closeQuickFilter();page.sequence++;
    $$('.flow-view-dialog').forEach(dialog => {dialog.close();dialog.remove();});
  }
  // Archiving only moves a row out of the working views; 全部 lists it with 恢复, so it needs no confirmation.
  async function archive(ids, archived) {
    if (page.archiving || !ids.length || !currentData()) return;
    page.archiving = true;clearTimeout(page.pollTimer);paintBatch();
    let revision = page.data.revision, done = 0, failure = null;
    try {
      for (const id of ids) {
        const result = await api('/api/content/manage',{method:'POST',body:{request_id:crypto.randomUUID(),revision,id,management:{archived}}});
        revision = result.revision;done += 1;
      }
    } catch (error) {failure = error;}
    finally {page.archiving = false;}
    const verb = archived ? '归档' : '恢复';
    if (failure) toast(`${done ? `已${verb} ${done} 项；` : ''}${ids.length - done} 项未${verb}：${errorText(failure)}`,true);
    else toast(archived ? `已归档${ids.length > 1 ? ` ${done} 项` : ''}，可在「全部」里恢复。` : '已恢复。');
    page.selecting = !!failure;page.selected = new Set(failure ? ids.slice(done) : []);paint();
    void load(++page.sequence);
  }
  // Deleting archived rows for good through each record's own delete. A script that an order, case,
  // posted video or plan still uses stays archived; the dialog says so and states exactly how many rows go.
  async function deleteArchived() {
    if (page.deleting || !currentData()) return;
    const scope = {product:page.product,view:page.filters.view,kind:page.filters.kind,status:page.filters.status,q:page.filters.q,...(page.filters.filter ? {filter:copy(page.filters.filter)} : {})};
    let preview;
    try {preview = await api('/api/content/delete-archived',{method:'POST',body:{...scope,preview:true}});}
    catch (error) {toast(errorText(error),true);return;}
    const total = preview.items.length, kept = preview.kept || {};
    const counts = preview.counts, parts = [['script','条脚本'],['task','条协作候选'],['video','张视频制作单'],['carousel','张图文制作单']].filter(([key]) => counts[key]).map(([key,unit]) => `${counts[key]} ${unit}`);
    const orders = counts.video || counts.carousel;
    const notes = [];
    if (kept.script) notes.push(`${kept.script} 条已归档的脚本被制作单、案例、视频 ID 或写稿计划用到，会保留在归档里。`);
    if (kept.busy) notes.push(`${kept.busy} 项还在运行，这次跳过。`);
    if (!total) {toast(notes[0] || '没有可以删除的归档内容。');return;}
    const label = `永久删除 ${total} 项`;
    const dialog = document.createElement('dialog');dialog.className = 'script-editor-dialog production-dialog flow-delete-dialog';
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>永久删除已归档内容</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div>
      <div class="script-editor-body"><p class="context-note">将永久删除 ${total} 项已归档内容：${esc(parts.join('、'))}。${counts.script ? '脚本会连同它的全部版本一起删除。' : ''}${orders ? '制作单在本地保存的配音、候选图、成图、成品包和发布登记会一并删除。' : ''}不能撤销。</p>
      <ul class="context-list"><li>达芬奇里的时间线和案例库不会改动${orders ? '；删掉的制作单用到的脚本会保留' : ''}。</li>${notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul>
      <details class="flow-delete-list"><summary>查看这 ${total} 项</summary><ul>${preview.items.map(item => `<li><span class="flow-kind">${esc(tags[item.kind] || item.kind)}</span><span>${esc(item.title || item.id)}</span></li>`).join('')}</ul></details>
      <label class="flow-delete-confirm"><input type="checkbox" name="sure" required>我已确认永久删除这 ${total} 项，不需要找回</label><p class="ai-error" data-error role="alert" hidden></p></div>
      <div class="dialog-footer"><span data-hint role="status" aria-live="polite"></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary" data-submit disabled>${label}</button></div></div></form>`;
    document.body.append(dialog);
    const close = () => {if (page.deleting) return;dialog.close();dialog.remove();};
    $$('[data-close]',dialog).forEach(button => button.onclick = close);
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    const submit = $('[data-submit]',dialog), hint = $('[data-hint]',dialog), failure = $('[data-error]',dialog);
    const sure = $('[name="sure"]',dialog);
    function updateControls() {
      sure.disabled = page.deleting;
      $$('[data-close]',dialog).forEach(node => node.disabled = page.deleting);
      submit.disabled = page.deleting || !sure.checked;
      submit.textContent = page.deleting ? '正在删除…' : label;
      hint.textContent = page.deleting ? '正在逐项删除，请不要关闭页面。' : !sure.checked ? '勾选确认后才能永久删除。' : '';
    }
    sure.onchange = updateControls;
    $('form',dialog).onsubmit = async event => {
      event.preventDefault();if (page.deleting || !sure.checked) return;
      page.deleting = true;clearTimeout(page.pollTimer);paintBatch();
      updateControls();failure.hidden = true;
      try {
        const result = await api('/api/content/delete-archived',{method:'POST',body:{...scope,ids:preview.items.map(item => item.id)}});
        page.deleting = false;close();
        const failed = result.results.filter(entry => !entry.ok), warned = result.results.flatMap(entry => entry.warnings || []);
        const message = [`已永久删除 ${result.deleted} 项`];
        if (failed.length) message.push(`${failed.length} 项没有删除：${failed[0].error}`);
        else if (warned.length) message.push(warned[0]);
        toast(message.join('，'),failed.length > 0 || warned.length > 0);
      } catch (error) {
        failure.textContent = errorText(error);failure.hidden = false;
      } finally {
        page.deleting = false;
        if (dialog.isConnected) updateControls();
        void load(++page.sequence);
      }
    };
    updateControls();dialog.showModal();
  }
  document.addEventListener('visibilitychange',() => {if (!document.hidden && $('#content-flow-list') && !page.archiving && !page.deleting) void load(++page.sequence);});
  return {route,tabs,captureListState,leaveList};
})();
