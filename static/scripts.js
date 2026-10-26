'use strict';

window.CaseScripts = (() => {
  const labels = {draft:'草稿',adopted:'已采纳',filmed:'已拍摄',published:'已发布',reviewed:'已复盘',archived:'已归档'};
  const workspace = {sequence:0,modal:null,loaded:null};
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const currentProduct = () => window.WorkbenchShell?.product() || '';
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const fail = error => error?.message || '操作暂未完成';
  const definitive = error => Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
  const scriptId = script => script.script_id || script.id;
  const scriptLink = script => url(`#content/${encodeURIComponent(scriptId(script))}`,script.product);
  const scriptDirection = script => script.direction_id || script.variant_link?.direction_id || '';
  const isArchived = script => script.archived === true || script.status === 'archived';
  const scriptStatus = script => script.lifecycle_status || (script.status === 'archived' ? 'draft' : script.status) || 'draft';
  const progressLabels = Object.fromEntries(Object.entries(labels).filter(([key]) => key !== 'archived'));
  function usageLabel(script) {
    if (!Array.isArray(script.production_usage)) return script.video_id ? '已登记视频 · 制作关联待核对' : '制作关联待核对';
    const usages = script.production_usage;
    const produced = usages.length ? `用于 ${usages.length} 张制作单` : '尚未用于制作';
    const releases = usages.filter(item => item.publication?.post_id).length;
    return `${produced}${releases ? ` · ${releases} 张已登记发布` : script.video_id ? ' · 已登记视频' : ''}`;
  }
  function variantAssociation(script) {
    const link = script.variant_link;
    if (!link || !Number.isInteger(link.resolved_index)) return '';
    return `<details class="dir-record-association"><summary>已核对关联 · 变体 ${link.resolved_index + 1}</summary><p>${esc(link.evidence?.note || '')}</p></details>`;
  }
  const field = (name,label,value = '',options = {}) => `<label class="field"><span>${esc(label)}</span>${options.area ? `<textarea name="${name}" rows="${options.rows || 5}" ${options.required ? 'required' : ''} ${options.readonly ? 'readonly' : ''}>${esc(value)}</textarea>` : `<input name="${name}" type="${options.type || 'text'}" value="${esc(value)}" ${options.required ? 'required' : ''}>`}</label>`;
  function modalOpen() {return !!workspace.modal;}
  function hasUnsavedInput() {return !!workspace.modal && (workspace.modal.busy || dirty());}
  function dirty() {const modal = workspace.modal;return !!modal && modal.initial !== JSON.stringify([...new FormData(modal.form).entries()]);}
  function closeModal(force = false) {
    const modal = workspace.modal;if (!modal) return;
    if (modal.busy) return;
    if (!force && (dirty() || modal.pending)) {askDiscard(() => closeModal(true));return;}
    modal.dialog.close();modal.dialog.remove();workspace.modal = null;
  }
  function askDiscard(discard) {
    if ($('#script-discard-dialog')) return;
    const dialog = document.createElement('dialog');dialog.id = 'script-discard-dialog';dialog.innerHTML = '<div class="confirm-body"><h2>保留这次编辑？</h2><p>输入尚未保存，可以继续编辑。提交结果未确认时，保留窗口可重试同一请求。</p><div class="confirm-actions"><button type="button" class="secondary" data-discard>放弃编辑</button><button type="button" class="primary" data-keep>继续编辑</button></div></div>';
    document.body.append(dialog);dialog.showModal();
    const finish = () => {dialog.close();dialog.remove();};
    $('[data-discard]',dialog).onclick = () => {finish();discard();};$('[data-keep]',dialog).onclick = finish;
    dialog.addEventListener('cancel',event => {event.preventDefault();finish();});
  }
  function beforeNavigate(hash) {
    const modal = workspace.modal;if (!modal || hash === modal.hash) return true;
    if (modal.busy || dirty() || modal.pending) {
      history.replaceState(null,'',modal.hash);
      if (!modal.busy) askDiscard(() => {closeModal(true);location.hash = hash;});
      return false;
    }
    closeModal(true);return true;
  }
  function showModal(title,body,button,onSubmit) {
    if (workspace.modal || window.CaseAI?.modalOpen?.() || state.editor) {toast('请先完成或关闭当前编辑。',true);return null;}
    const dialog = document.createElement('dialog');dialog.id = 'script-editor-dialog';dialog.className = 'script-editor-dialog';
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>${esc(title)}</h2><button type="button" class="icon-button" data-close aria-label="关闭脚本编辑">${icon('close')}</button></div><div class="script-editor-body">${body}<p class="ai-error" data-error role="alert" hidden></p></div><div class="dialog-footer"><span data-hint>保存后保留来源与历史版本。</span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary" data-submit>${esc(button)}</button></div></div></form>`;
    document.body.append(dialog);
    const form = $('form',dialog), modal = {dialog,form,hash:location.hash,busy:false,pending:null,button,onSubmit,initial:JSON.stringify([...new FormData(form).entries()])};
    workspace.modal = modal;
    $$('[data-close]',dialog).forEach(button => button.onclick = () => closeModal());
    dialog.addEventListener('cancel',event => {event.preventDefault();closeModal();});
    form.onsubmit = event => {event.preventDefault();if (!modal.busy) void onSubmit(modal);};
    dialog.showModal();return modal;
  }
  function setError(modal,message) {const node = $('[data-error]',modal.dialog);node.textContent = message;node.hidden = !message;}
  function setBusy(modal,busy) {
    modal.busy = busy;
    $$('input,textarea,select,button',modal.form).forEach(node => {node.disabled = busy || (!!modal.pending && !node.matches('[data-close],[data-submit]'));});
    $('[data-submit]',modal.form).textContent = busy ? '正在保存…' : modal.pending ? '重试同一请求' : modal.button;
  }
  async function post(modal,path,body) {
    if (!modal.pending) modal.pending = {path,body:{...body,request_id:crypto.randomUUID()}};
    setError(modal,'');setBusy(modal,true);
    try {const result = await api(modal.pending.path,{method:'POST',body:modal.pending.body});modal.pending = null;return result;}
    catch (error) {if (definitive(error)) modal.pending = null;throw error;}
    finally {setBusy(modal,false);}
  }
  async function catalog(product) {return api(`/api/creation?product=${encodeURIComponent(product || '')}`);}
  async function handleSaveError(modal,error) {
    if (error.status === 409) {
      try {modal.revision = (await catalog(modal.product)).revision;}
      catch {setError(modal,`记录已更新，暂时无法重新读取。输入仍保留：${fail(error)}`);return;}
      setError(modal,`记录已更新，本次没有保存。已读取最新版本，请核对后再次提交。${fail(error)}`);
    } else setError(modal,definitive(error) ? fail(error) : `保存结果尚未确认：${fail(error)}。请重试同一请求核对。`);
  }
  // Adopted copy belongs to its content; old scripts URLs are normalized by the shell.
  async function route(keepScroll = false) {
    if (keepScroll && workspace.modal) return;
    const hash = pageHash(), seq = ++workspace.sequence;
    const tail = hash.replace(/^#(?:content|scripts)\/?/,'').split('?')[0];
    if (!tail) {location.hash = url('#content');return;}
    $('#breadcrumb').textContent = '内容创作 / 内容详情';
    if (!keepScroll || !$('#content-adopted-script')) $('#content').innerHTML = skeletonMarkup(2);
    try {
      const data = await api(`/api/scripts/${encodeURIComponent(decodeURIComponent(tail))}`);
      if (seq !== workspace.sequence || hash !== pageHash()) return;
      const script = data.script;
      if (currentProduct() && !window.WorkbenchShell.matchesProduct(script.product)) {location.hash = scriptLink(script);return;}
      const signature = JSON.stringify(data);
      workspace.loaded = data;
      if (keepScroll && workspace.detailHash === hash && workspace.detailSignature === signature && $('#content-adopted-script')) {
        await adoptedMounts.get($('#content-adopted-script'))?.refresh?.();return;
      }
      workspace.detailHash = hash;workspace.detailSignature = signature;renderDetail(data);
      const table = new URLSearchParams(hash.split('?').slice(1).join('?')).get('table');
      const target = `${hash}:${table || ''}`;
      if (table && (!keepScroll || workspace.tableResultTarget !== target)) {
        workspace.tableResultTarget = target;void resumeTableResult(table,scriptId(script));
      }
      if (!keepScroll) window.scrollTo(0,0);
    } catch (error) {
      if (seq !== workspace.sequence || hash !== pageHash()) return;
      if (error.status === 404) {
        $('#content').innerHTML = `<div class="empty"><h3>这份采用稿已不存在</h3><p>可能已被删除，或属于其他产品。</p><a class="secondary" href="${esc(url('#content'))}">返回内容列表</a></div>`;return;
      }
      $('#content').innerHTML = `<div class="empty"><h3>暂时无法读取内容</h3><p>${esc(fail(error))}</p><button class="secondary" id="script-retry">重新读取</button></div>`;
      $('#script-retry').onclick = () => route(true);
    }
  }
  function captureListState() {return null;}
  const adoptedMounts = new WeakMap(), taskMounts = new WeakMap();
  function familyVersions(data,script) {
    const family = script.family_id || scriptId(script);
    const rows = (data.scripts || []).filter(row => (row.family_id || scriptId(row)) === family && row.product === script.product);
    if (!rows.some(row => scriptId(row) === scriptId(script))) rows.push(script);
    return rows.sort((a,b) => (Number(b.version) || 1) - (Number(a.version) || 1) || String(b.created_at || '').localeCompare(String(a.created_at || '')));
  }
  function copyMarkup(script) {
    return `<pre>${esc(script.text || '')}</pre>${script.translation_zh ? `<h3>中文对照</h3><pre>${esc(script.translation_zh)}</pre>` : ''}`;
  }
  function usageMarkup(script) {
    return `<p class="context-note">${esc(usageLabel(script))}</p>${(script.production_usage || []).map(use => `<a class="script-use-link" href="${esc(url(`#production/${encodeURIComponent(use.id)}`,script.product))}">${use.kind === 'carousel' ? '图文' : '视频'}制作单 · ${esc(use.id)}${use.publication?.post_id ? ` · 已发布 ${esc(use.publication.post_id)}` : ''}${use.archived ? ' · 已归档' : ''}</a>`).join('')}`;
  }
  function paintVersions(host,data,script,options = {}) {
    const rows = familyVersions(data,script), current = scriptId(script);
    const signature = JSON.stringify([rows,options.productionId || '']);
    if (host._versionSignature === signature && $('[data-script-versions]',host)) return;
    const expanded = new Set($$('[data-script-version][open]',host).map(node => node.dataset.scriptVersion));
    const historyOpen = !!$('[data-script-versions][open]',host);
    host._versionSignature = signature;
    // An order whose script has a single version needs only the way to its version page, not a one-row history.
    if (options.productionId && rows.length <= 1) {host.innerHTML = `<a class="adopted-version-detail adopted-version-single" data-adopted-version-detail href="${esc(scriptLink(script))}">采用稿版本详情</a>`;return;}
    host.innerHTML = `<details class="adopted-history" data-script-versions><summary>版本历史（${rows.length}）</summary><p class="context-note">采用和修改时自动保留。每张制作单始终对应实际使用的稿件。</p><div class="adopted-version-list">${rows.map(row => `<details data-script-version="${esc(scriptId(row))}"><summary>第 ${esc(row.version || 1)} 版${scriptId(row) === current ? options.productionId ? ' · 本次采用' : options.requestedId ? ' · 正在查看' : ' · 当前采用稿' : ''}${isArchived(row) ? ' · 已归档' : ''}<small>${esc(readableDate(row.created_at))}</small></summary><div class="adopted-version-body">${copyMarkup(row)}${usageMarkup(row)}${scriptId(row) !== current || options.productionId ? `<a class="adopted-version-detail" data-adopted-version-detail href="${esc(scriptLink(row))}">${options.productionId ? '采用稿版本详情' : '查看这版内容'}</a>` : ''}</div></details>`).join('')}</div></details>`;
    $('[data-script-versions]',host).open = historyOpen;
    $$('[data-script-version]',host).forEach(node => {node.open = expanded.has(node.dataset.scriptVersion);});
  }
  async function mountAdopted(container,script,options = {}) {
    if (!container || !script) return;
    const token = {};adoptedMounts.set(container,token);
    const current = () => container.isConnected && adoptedMounts.get(container) === token;
    container.classList.add('adopted-script');container.dataset.adoptedScriptId = scriptId(script);
    // Production responses contain a script reference, while the content API contains its full text.
    // Resolve the exact reference before enabling copy/edit or deriving the family from it.
    let initialData = options.catalogData;
    if (!Object.prototype.hasOwnProperty.call(script,'text')) {
      container.innerHTML = '<p class="context-note" role="status">正在读取采用稿…</p>';
      try {
        initialData ||= await catalog(script.product);
        if (!current()) return;
        const full = (initialData.scripts || []).find(row => scriptId(row) === scriptId(script) && row.product === script.product);
        if (!full) throw new Error('没有找到这张制作单对应的采用稿。');
        script = full;
      } catch (error) {
        if (!current()) return;
        container.innerHTML = `<p class="context-note" role="status">采用稿暂时无法读取：${esc(fail(error))}</p><button type="button" class="secondary compact" data-adopted-retry>重新读取</button>`;
        $('[data-adopted-retry]',container).onclick = () => void mountAdopted(container,script,options);return;
      }
    }
    // In an order the lines table already carries every sentence and its Chinese, so the full copy waits behind a fold.
    const copy = `<div class="adopted-copy">${copyMarkup(script)}</div>`;
    const hint = '<details class="production-hint"><summary title="说明" aria-label="说明"></summary><div class="production-hint-body">这张制作单使用以下稿件。修改会自动保留原稿，并生成新的待制作内容。</div></details>';
    container.innerHTML = `<div class="section-title-row"><h2>${options.productionId ? `本次采用稿 · 第 ${esc(script.version || 1)} 版${hint}` : `采用稿 <small>第 ${esc(script.version || 1)} 版</small>`}</h2><div class="adopted-actions"><button type="button" class="subtle" data-adopted-copy>${icon('copy')}复制正文</button>${options.editable === false ? '' : '<button type="button" class="subtle" data-adopted-edit>编辑采用稿</button>'}</div></div>${options.productionId ? `<details class="adopted-copy-fold" data-adopted-copy-fold><summary>查看正文</summary>${copy}</details>` : copy}<div data-adopted-history><p class="context-note" role="status">正在读取版本历史…</p></div>`;
    $('[data-adopted-copy]',container).onclick = () => copyText(script.text || '','采用稿已复制');
    $('[data-adopted-edit]',container)?.addEventListener('click',() => void openManual(script));
    const host = $('[data-adopted-history]',container);
    let historySequence = 0;
    async function loadHistory(provided) {
      const sequence = ++historySequence;
      try {
        const data = provided || await catalog(script.product);
        if (current() && sequence === historySequence) paintVersions(host,data,script,options);
      } catch (error) {
        if (!current() || sequence !== historySequence) return;
        host.innerHTML = `<p class="context-note" role="status">版本历史暂时无法读取：${esc(fail(error))}</p><button type="button" class="secondary compact" data-history-retry>重新读取</button>`;
        $('[data-history-retry]',host).onclick = () => void loadHistory();
      }
    }
    token.refresh = () => loadHistory();
    await loadHistory(initialData);
  }
  function taskProductionMarkup(script) {
    const uses = (script.production_usage || []).filter(use => !use.archived && use.status !== 'archived');
    const production = uses.length ? uses.map(use => `<a class="secondary compact task-adopted-production" data-task-production href="${esc(url(`#production/${encodeURIComponent(use.id)}`,script.product))}"><span>继续制作${use.kind === 'carousel' ? '图文' : '视频'}</span>${uses.length > 1 ? `<small>${esc(use.id)}</small>` : ''}</a>`).join('') : '<button type="button" class="secondary compact" data-task-production>开始制作</button>';
    return `<div class="task-adopted-actions">${production}<a class="adopted-version-detail" data-adopted-version-detail href="${esc(scriptLink(script))}">版本详情</a></div>`;
  }
  async function mountTaskAdopted(container,task,options = {}) {
    if (!container || !task) return;
    const requestedId = options.requestedId ?? new URLSearchParams(pageHash().split('?').slice(1).join('?')).get('adopted') ?? '';
    const signature = JSON.stringify([task.task_id,task.revision,task.updated_at,task.adopted_script_count,requestedId]);
    const old = taskMounts.get(container);
    if (old?.signature === signature && (old.pending || Date.now() - old.at < 30000)) return;
    const token = {signature,requestedId,pending:true,at:Date.now()};taskMounts.set(container,token);
    if (old?.requestedId !== requestedId) delete container.dataset.focusedAdopted;
    try {
      const data = await catalog(task.product || task.context?.product);
      if (!container.isConnected || taskMounts.get(container) !== token) return;
      const families = new Map();
      for (const script of data.scripts || []) {
        if (script.source?.task_id !== task.task_id) continue;
        const key = script.family_id || scriptId(script), old = families.get(key);
        if (!old || (Number(script.version) || 1) > (Number(old.version) || 1)) families.set(key,script);
      }
      const requested = requestedId ? (data.scripts || []).find(script => scriptId(script) === requestedId && script.source?.task_id === task.task_id) : null;
      if (requested) families.set(requested.family_id || scriptId(requested),requested);
      const scripts = [...families.values()];keepReadingPlace(() => {container.hidden = !scripts.length && !requestedId;});
      token.renderSignature = JSON.stringify([requestedId,(data.scripts || []).filter(row => row.source?.task_id === task.task_id)]);
      if (old?.renderSignature === token.renderSignature && container.children.length) {
        token.pending = false;token.at = Date.now();return;
      }
      const retained = new Map($$('.task-adopted-copy',container).map(node => {
        const body = $('[data-task-adopted]',node);
        return [node.dataset.taskFamily,{open:node.open,history:!!$('[data-script-versions][open]',body),
          versions:new Set($$('[data-script-version][open]',body).map(version => version.dataset.scriptVersion))}];
      }));
      keepReadingPlace(() => {container.hidden = !scripts.length && !requestedId;
      container.innerHTML = `${requestedId && !requested ? '<p class="context-note" role="status" data-requested-adopted-missing>指定的采用稿在这条写稿记录中已不可读。</p>' : ''}${scripts.length ? `<h2>已保存的采用稿</h2>${scripts.map(script => `<details class="task-adopted-copy" data-task-family="${esc(script.family_id || scriptId(script))}"><summary>${esc(script.title || '采用稿')} · 第 ${esc(script.version || 1)} 版</summary><div data-task-adopted="${esc(scriptId(script))}"></div>${taskProductionMarkup(script)}</details>`).join('')}` : ''}`;});
      scripts.forEach(script => {
        const body = $(`[data-task-adopted="${scriptId(script)}"]`,container);
        const saved = retained.get(script.family_id || scriptId(script));
        if (saved) body.closest('.task-adopted-copy').open = saved.open;
        $('[data-task-production]:is(button)',body.closest('.task-adopted-copy'))?.addEventListener('click',() => window.CaseProduction?.fromScript(script));
        void mountAdopted(body,script,{catalogData:data,requestedId:requestedId === scriptId(script) ? requestedId : ''}).then(() => {
          if (!body.isConnected || taskMounts.get(container) !== token) return;
          if (saved) {
            const history = $('[data-script-versions]',body);if (history) history.open = saved.history;
            $$('[data-script-version]',body).forEach(node => {node.open = saved.versions.has(node.dataset.scriptVersion);});
          }
          if (requestedId !== scriptId(script) || new URLSearchParams(pageHash().split('?').slice(1).join('?')).get('adopted') !== requestedId) return;
          body.closest('.task-adopted-copy').open = true;
          const focusKey = `${task.task_id}:${requestedId}`;
          if (container.dataset.focusedAdopted === focusKey) return;
          container.dataset.focusedAdopted = focusKey;body.tabIndex = -1;body.focus({preventScroll:true});body.scrollIntoView({block:'start'});
        });
      });
      token.pending = false;token.at = Date.now();
    } catch (error) {
      if (!container.isConnected || taskMounts.get(container) !== token) return;
      taskMounts.delete(container);
      keepReadingPlace(() => {container.hidden = false;
        container.innerHTML = `<p class="context-note">采用稿暂时无法读取：${esc(fail(error))}</p><button type="button" class="secondary compact" data-task-adopted-retry>重新读取</button>`;});
      $('[data-task-adopted-retry]',container).onclick = () => void mountTaskAdopted(container,task);
    }
  }
  function renderDetail(data) {
    const script = data.script, id = scriptId(script), source = script.source || {};
    const performanceItem = {sales_data:data.sales_data || {status:script.video_id ? 'not_found' : 'missing_video_id'},current:{metrics:{video_id:script.video_id || ''}}};
    const versionMetadata = `<details class="section-card script-version-meta"><summary>版本记录</summary><dl class="script-facts"><dt>保存时间</dt><dd>${esc(readableDate(script.created_at))}</dd>${source.task_id ? '' : '<dt>录入方式</dt><dd>手动整理的正文</dd>'}</dl>${variantAssociation(script)}${script.note ? `<p>${esc(script.note)}</p>` : ''}</details>`;
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#content',script.product))}">${icon('back')}返回内容列表</a><div class="page-heading"><div><span class="eyebrow">ADOPTED COPY</span><h1>${esc(script.title || '未命名脚本')}</h1><p>${esc(script.product)} · ${esc(scriptDirection(script) || '未指定方向')}${script.variant_link ? ` · 变体 ${script.variant_link.resolved_index + 1}` : ''} · <span class="script-status ${esc(scriptStatus(script))}">${esc(labels[scriptStatus(script)] || scriptStatus(script))}${isArchived(script) ? ' · 已归档' : ''}</span></p></div><div class="script-detail-actions"><button class="primary" id="script-production-toggle" type="button" aria-expanded="false" aria-controls="script-production-choices" disabled>正在读取制作单…</button><details class="script-more-actions" id="script-more-actions"><summary>更多操作</summary><div><button class="subtle" id="script-derive">编辑采用稿</button><button class="subtle" id="script-archive">${isArchived(script) ? '恢复采用稿' : '归档采用稿'}</button><button class="subtle" id="script-case">${script.case_id ? '补充案例结论' : '沉淀为案例'}</button></div></details></div></div><p class="context-note script-production-note" id="script-production-note" role="status" hidden></p><section class="section-card script-production-choices" id="script-production-choices" aria-label="选择制作单或制作形式" hidden></section><div class="script-detail-grid"><section class="section-card script-body" id="content-adopted-script"></section><aside><section class="section-card"><h2>拍摄与发布</h2><dl class="script-facts"><dt>采用稿编号</dt><dd>${esc(id)}</dd><dt>视频 ID</dt><dd><span>${esc(script.video_id || '尚未登记')}</span>${copyIdButton(script.video_id,'视频 ID')}</dd><dt>发布日期</dt><dd>${esc(script.published_on || '尚未登记')}</dd></dl><button class="secondary" id="script-release">登记进度与视频</button><p class="context-note" id="script-release-note">同一视频按视频 ID 匹配销售数据。拍摄不同版本时请分别登记。</p></section></aside></div>${versionMetadata}<section id="script-sales">${typeof renderPerformanceSection === 'function' ? renderPerformanceSection(performanceItem,'script') : ''}</section>`;
    // Per-slide tables are generated from this script; their runs show as one line under the heading.
    const runs = document.createElement('div');runs.id = 'script-activity-logs';runs.className = 'task-activity-logs';$('.page-heading',$('#content')).after(runs);
    window.TaskActivity?.mountLogs(runs,{owner_id:id,label:'本脚本'});
    window.WorkbenchLinks?.mount({kind:'script',record:script,script});
    void mountAdopted($('#content-adopted-script'),script,{editable:false});
    $('#script-derive').onclick = () => openManual(script);
    $('#script-release').onclick = () => openStatus(script,data.revision);
    $('#script-archive').onclick = () => openArchive(script,data.revision);
    $('#script-case').onclick = () => openCase(script,script.case_id || '');
    if (source.case_id && source.case_id !== script.case_id) {
      $('#script-case').insertAdjacentHTML('afterend','<button class="subtle" id="script-write-source">写回参考案例</button>');
      $('#script-write-source').onclick = () => openCase(script,source.case_id);
    }
    const history = script.history || [];
    if (history.length) $('#script-sales').insertAdjacentHTML('beforebegin',`<details class="section-card script-history"><summary>查看进度记录（${history.length} 条）</summary><ol>${history.map(entry => `<li><strong>${esc(entry.event === 'archive' ? '归档' : entry.event === 'restore' ? '恢复' : labels[entry.status] || entry.status)}</strong> · ${esc(readableDate(entry.at))}${entry.video_id ? ` · 视频 ${esc(entry.video_id)}` : ''}${entry.note ? `<p>${esc(entry.note)}</p>` : ''}</li>`).join('')}</ol></details>`);
    const more = $('#script-more-actions');
    $$('button',more).forEach(button => button.addEventListener('click',() => {more.open = false;}));
    void loadScriptProduction(script);
  }
  function canResumeTableResult() {
    return !workspace.modal && !state.editor && !window.CaseAI?.modalOpen?.() && !window.CaseProduction?.modalOpen?.() && !window.CaseDirectionPicker?.hasVideoEdits?.();
  }
  async function resumeTableResult(tableId,expectedScriptId = '') {
    if (!canResumeTableResult()) {toast('请先完成或关闭当前编辑，再查看逐张表。',true);return false;}
    const script = workspace.loaded?.script, tail = pageHash().replace(/^#(?:content|scripts)\/?/,'').split('?')[0];
    if (!script || tail !== scriptId(script) || (expectedScriptId && expectedScriptId !== scriptId(script))) return false;
    await window.CaseProduction?.resumeCarouselTable(script,tableId);
    return true;
  }
  async function loadScriptProduction(script) {
    const toggle = $('#script-production-toggle'), panel = $('#script-production-choices'), note = $('#script-production-note');
    const hash = pageHash(), seq = workspace.sequence;
    toggle.disabled = true;toggle.textContent = '正在读取制作单…';toggle.setAttribute('aria-expanded','false');panel.hidden = true;panel.innerHTML = '';
    note.hidden = true;note.textContent = '';
    try {
      const data = await api(`/api/production?product=${encodeURIComponent(script.product || '')}`);
      if (seq !== workspace.sequence || hash !== pageHash() || !toggle.isConnected) return;
      if (!Array.isArray(data.jobs) || !Array.isArray(data.carousels)) throw new Error('制作单列表不完整，请重新读取。');
      const sameScript = item => item.script_id === scriptId(script) && (!item.product || window.WorkbenchShell.matchesProduct(item.product,script.product)) && !item.archived && item.status !== 'archived';
      const existing = [...data.jobs.filter(sameScript).map(item => ({...item,kind:'video'})),...data.carousels.filter(sameScript).map(item => ({...item,kind:'carousel'}))]
        .sort((a,b) => String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || '')));
      const videoLabels = {draft:'待配音',voiced:'已配音',timeline:'已建时间线',editing:'剪辑中',awaiting_review:'待人工审查',approved:'人工审查通过',needs_material:'待补素材',needs_verification:'素材待核实',needs_repair:'待修正',verification_failed:'核验未完成',unverified:'旧记录待核验',completed:'旧记录待核验',failed:'剪辑失败',cancelled:'剪辑已停止',interrupted:'剪辑已中断'};
      const carouselLabels = {draft:'待选底图',picking:'选图中',picked:'底图已选齐',ready:'底图已选齐',reviewing:'成图待核对',approved:'成图已确认',published:'已登记发布'};
      const create = '<div class="script-production-create"><button type="button" class="secondary" id="script-produce">制作视频</button></div>';
      panel.innerHTML = existing.length ? `<h2>关联制作单 <small>${existing.length} 张</small></h2><p class="context-note">选择要接着处理的制作单。</p><div class="script-production-links">${existing.map(item => {
        const carousel = item.kind === 'carousel', status = (carousel ? carouselLabels : videoLabels)[item.status] || '查看进度';
        const href = url(`#production/${encodeURIComponent(item.id)}`,item.product || script.product);
        return `<a class="script-production-link" href="${esc(href)}"><span><strong>${esc(item.display_title || item.title || script.title || '未命名制作单')}</strong><small>${carousel ? '图文' : '视频'} · ${esc(item.id)}${item.updated_at || item.created_at ? ` · ${esc(readableDate(item.updated_at || item.created_at))}` : ''}</small></span><span>${esc(status)}${item.status === 'awaiting_review' && !carousel ? ' · 去审查' : ' · 打开'}</span></a>`;
      }).join('')}</div><details class="script-production-new"><summary>新建另版</summary><p class="context-note">已有制作单会保留，另建一张用于不同的制作版本。</p>${create}</details>` : `<h2>制作视频</h2><p class="context-note">这条脚本还没有未归档的制作单。核对内容后创建视频制作单。</p>${create}`;
      toggle.textContent = existing.length ? '继续制作' : '开始制作';toggle.disabled = false;
      // With an order, the video ID is registered in the order's publish step, which writes it back here;
      // a second entry on this page could register a different ID. The manual form stays one click away.
      const release = $('#script-release'), releaseNote = $('#script-release-note');
      if (release && existing.length) {
        const only = existing.length === 1 ? existing[0] : null;
        release.textContent = '去制作单登记发布';release.className = 'primary';
        release.onclick = () => {
          if (only) {location.hash = url(`#production/${encodeURIComponent(only.id)}?step=${only.kind === 'carousel' ? 'delivery' : 'publish'}`,only.product || script.product);return;}
          panel.hidden = false;toggle.setAttribute('aria-expanded','true');panel.scrollIntoView({block:'nearest'});
        };
        if (releaseNote) releaseNote.innerHTML = `${only ? '' : `这条脚本有 ${existing.length} 张制作单，选实际发布的那张。`}在制作单的发布步骤填视频 ID，会自动写回这里。<button type="button" class="subtle compact" id="script-release-manual">不经制作单，手动登记</button>`;
        $('#script-release-manual')?.addEventListener('click',() => openStatus(script,workspace.loaded?.revision));
      }
      toggle.onclick = () => {panel.hidden = !panel.hidden;toggle.setAttribute('aria-expanded',String(!panel.hidden));};
      $('#script-produce').onclick = () => window.CaseProduction?.fromScript(script);
    } catch (error) {
      if (seq !== workspace.sequence || hash !== pageHash() || !toggle.isConnected) return;
      note.textContent = `暂时无法核对已有制作单：${fail(error)}。重新读取后可继续或新建。`;note.hidden = false;
      toggle.textContent = '重新读取制作单';toggle.disabled = false;toggle.onclick = () => void loadScriptProduction(script);
    }
  }
  async function openManual(parent = null) {
    const origin = location.hash;
    const product = parent?.product || currentProduct();if (!product) {toast('请先选择一个具体产品。',true);return;}
    try {
      const [data,directions] = await Promise.all([catalog(product),api(`/api/direction-catalog?product=${encodeURIComponent(product)}`)]);
      if (location.hash !== origin) return;
      const body = `<p class="context-note">${parent ? '保存修改时自动保留上一稿；已有制作单继续使用原稿。' : '保存准备采用的正文，可稍后开始制作。'}</p>${field('title','脚本标题',parent?.title || '',{required:true})}<label class="field"><span>关联方向</span><select name="direction_id"><option value="">暂不指定</option>${(directions.directions || []).filter(item => item.status !== 'archived' || item.direction_id === parent?.direction_id).map(item => `<option value="${esc(item.direction_id)}" ${item.direction_id === parent?.direction_id ? 'selected' : ''}>${esc(item.direction_id)} · ${esc(item.main_difference)}</option>`).join('')}</select></label>${field('text','采用正文',parent?.text || '',{area:true,rows:10,required:true})}${field('translation_zh','中文对照',parent?.translation_zh || '',{area:true})}`;
      const modal = showModal(parent ? '编辑采用稿' : '手动录入脚本',body,'保存采用稿',async modal => {
        const values = Object.fromEntries(new FormData(modal.form));
        try {const result = await post(modal,'/api/scripts',{revision:modal.revision,script:{...values,product,confirmed:true,...(parent ? {parent_id:scriptId(parent)} : {})}});const saved = result.script;closeModal(true);location.hash = scriptLink(saved);toast(parent ? '采用稿已更新，原稿和已有制作记录已保留。' : '采用稿已保存，可直接开始制作。');}
        catch (error) {await handleSaveError(modal,error);}
      });if (modal) {modal.revision = data.revision;modal.product = product;}
    } catch (error) {toast(`暂时无法打开脚本编辑：${fail(error)}`,true);}
  }
  async function adoptCandidate(task,round,writeBack = false) {
    if (round?.status !== 'completed' || !round.output?.trim()) {toast('只有完整完成的候选才可以采纳。',true);return;}
    const product = task.product || task.context?.product || (state.cases || []).find(item => item.case_id === task.case_id)?.product || currentProduct();
    try {
      const data = await catalog(product), direction = task.context?.direction_id || task.direction_id || '';
      const title = task.case_title || task.context?.title || direction || `${product} 写稿`;
      const body = `<p class="context-note">从完整候选中选取一条准备采用的脚本，粘贴到「采用正文」。中文放入对照栏；多稿说明与评分不自动写入正文。</p><details open class="script-candidate-source"><summary>完整候选 · 第 ${(task.rounds || []).findIndex(item => item.round_id === round.round_id)+1} 轮</summary><pre>${esc(round.output)}</pre><button type="button" class="secondary" id="script-use-selection">把选中的候选文字填入正文</button></details>${field('title','脚本标题',title,{required:true})}${field('text','采用正文','',{area:true,rows:9,required:true})}${field('translation_zh','中文对照','',{area:true})}<p class="context-note">${esc(product)}${direction ? ` · ${esc(direction)}` : ''} · 保存完整候选来源，正文以你核对的内容为准。</p>`;
      const modal = showModal(writeBack ? '核对正文后写回案例' : '保存采用稿',body,writeBack ? '核对并继续写回' : '保存采用稿',async modal => {
        const values = Object.fromEntries(new FormData(modal.form));
        try {
          const result = await post(modal,'/api/scripts',{revision:modal.revision,script:{...values,product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:modal.taskRevision}});
          const saved = result.script;closeModal(true);toast(result.reused ? '这条候选之前已采纳，已打开原来的采用稿。' : '采用稿已保存，来源已自动保留。');
          if (writeBack) await openCase(saved,task.case_id || '');else location.hash = scriptLink(saved);
        } catch (error) {
          await handleSaveError(modal,error);
          if (error.status === 409) {try {const fresh = await api(`/api/ai/tasks/${encodeURIComponent(task.task_id)}`), current = fresh.task || fresh, found = current.rounds?.find(item => item.round_id === round.round_id);if (found?.status === 'completed' && found.output === round.output) modal.taskRevision = current.revision;} catch {}}
        }
      });
      if (!modal) return;modal.revision = data.revision;modal.product = product;modal.taskRevision = task.revision;
      let selected = '';
      const source = $('.script-candidate-source pre',modal.dialog);
      source.addEventListener('mouseup',() => {const selection = window.getSelection();if (selection && source.contains(selection.anchorNode) && source.contains(selection.focusNode)) selected = selection.toString();});
      $('#script-use-selection').onclick = () => {if (!selected.trim()) {setError(modal,'请先在上方候选中选中一条脚本的正文，或直接复制粘贴到正文栏。');return;}$('[name="text"]',modal.form).value = selected.trim();setError(modal,'');};
    } catch (error) {toast(`暂时无法采纳候选：${fail(error)}`,true);}
  }
  function openArchive(script,revision) {
    const archived = !isArchived(script);
    const body = `<p>${esc(script.title || '未命名脚本')} · 第 ${esc(script.version || 1)} 版</p><p class="context-note">${archived ? '归档后移入历史，正文、进度及制作和发布关联完整保留。' : '恢复后重新出现在当前活跃稿件中，原有进度和关联继续保留。'}</p>${field('note','整理备注','',{area:true,rows:3})}`;
    const modal = showModal(archived ? '归档采用稿' : '恢复采用稿',body,archived ? '归档脚本' : '恢复脚本',async modal => {
      try {
        await post(modal,`/api/scripts/${encodeURIComponent(scriptId(script))}/status`,{revision:modal.revision,script_status:{archived,note:$('[name="note"]',modal.form).value.trim()}});
        closeModal(true);await route(true);toast(archived ? '脚本已归档，可在历史中恢复。' : '脚本已恢复。');
      } catch (error) {await handleSaveError(modal,error);}
    });
    if (modal) {modal.revision = revision;modal.product = script.product;}
  }
  function openStatus(script,revision) {
    const body = `<p class="context-note">${esc(script.title)} · ${esc(scriptId(script))}</p><label class="field"><span>当前状态</span><select name="status">${Object.entries(progressLabels).map(([key,label]) => `<option value="${key}" ${scriptStatus(script) === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>${field('video_id','视频 ID',script.video_id || '')}${field('published_on','发布日期',script.published_on || '',{type:'date'})}${field('note','本次进度备注','',{area:true,rows:3})}`;
    const modal = showModal('登记进度与视频',body,'保存进度',async modal => {
      const values = Object.fromEntries(new FormData(modal.form));
      try {await post(modal,`/api/scripts/${encodeURIComponent(scriptId(script))}/status`,{revision:modal.revision,script_status:values});closeModal(true);await route(true);toast('脚本进度与视频关联已保存。');}
      catch (error) {await handleSaveError(modal,error);if (error.status === 409) {try {const fresh = await api(`/api/scripts/${encodeURIComponent(scriptId(script))}`);modal.revision = fresh.revision;setError(modal,`记录已更新，本次未保存。当前记录：${labels[fresh.script.status] || fresh.script.status}；视频 ${fresh.script.video_id || '未登记'}；发布日期 ${fresh.script.published_on || '未登记'}。你的输入仍保留，请核对后再保存。`);} catch {}}}
    });if (modal) {modal.revision = revision;modal.product = script.product;window.VideoPublication?.followPostDate?.(modal.form,'video_id');}
  }
  function caseText(item) {return item?.current?.transcript?.text || item?.current?.script?.text || item?.current?.script?.original || item?.current?.script?.source_text || item?.current?.script_text || '';}
  async function openCase(script,caseId = '') {
    try {
      const [data,item] = await Promise.all([catalog(script.product),caseId ? api(`/api/cases/${encodeURIComponent(caseId)}`) : Promise.resolve(null)]);
      const oldVideo = item?.current?.metrics?.video_id || '', matches = !caseId || oldVideo === (script.video_id || '');
      const body = `<p class="context-note">${caseId ? `参考案例「${esc(item.current?.title || caseId)}」。写回会新增修订并保留历史。` : '使用脚本、方向与视频资料创建案例；补一句你的结论即可。'}</p>${caseId ? `<label class="field"><span>保存到哪里</span><select name="destination"><option value="existing" ${matches ? 'selected' : 'disabled'}>写回已有案例</option><option value="new" ${matches ? '' : 'selected'}>另存为新案例</option></select></label>${matches ? '' : `<p class="ai-options-note">新稿${script.video_id ? `登记的视频 ${esc(script.video_id)}` : '尚未登记视频'}与参考案例的视频 ${esc(oldVideo || '未登记')}不同。可以另存为新案例；如果这是该视频实际采用的稿，请先到<a href="${esc(scriptLink(script))}">脚本详情登记同一视频</a>后再写回。</p>`}` : ''}<details class="script-case-comparison" ${caseId ? 'open' : ''}><summary>核对正文${caseId ? '与原案例' : ''}</summary>${caseId ? `<h3>案例当前正文</h3><pre data-original>${esc(caseText(item) || '当前案例未记录正文')}</pre>` : ''}<h3>本次采用正文</h3><pre>${esc(script.text)}</pre>${script.translation_zh ? `<h3>中文对照</h3><pre>${esc(script.translation_zh)}</pre>` : ''}</details>${field('conclusion','本次结论','',{area:true,required:true,rows:4})}<p class="context-note">方向：${esc(script.direction_id || '未指定')} · 视频：${esc(script.video_id || '尚未登记')} · 版本：${esc(scriptId(script))}</p>`;
      const modal = showModal(caseId ? '写回案例' : '沉淀为案例',body,caseId && matches ? '保存为案例修订' : '创建案例',async modal => {
        const conclusion = $('[name="conclusion"]',modal.form).value.trim();
        const targetCase = caseId && $('[name="destination"]',modal.form)?.value === 'existing' ? caseId : '';
        try {
          const result = await post(modal,`/api/scripts/${encodeURIComponent(scriptId(script))}/case`,{revision:modal.revision,...(targetCase ? {case_id:targetCase,case_revision:modal.caseRevision} : {}),conclusion,title:script.title});
          const savedId = result.case?.case_id || result.case_id || result.script?.case_id;
          closeModal(true);if (savedId) location.hash = url(`#case/${encodeURIComponent(savedId)}`,script.product);else location.hash = scriptLink(script);toast(result.warning || '案例已保存，脚本和来源已关联。',!!result.warning);
        } catch (error) {
          await handleSaveError(modal,error);
          if (error.status === 409 && targetCase) {try {const fresh = await api(`/api/cases/${encodeURIComponent(targetCase)}`);modal.caseRevision = fresh.revision;$('[data-original]',modal.form).textContent = caseText(fresh) || '当前案例未记录正文';setError(modal,`案例或脚本资料已更新，本次未写回。上方已显示案例最新正文，请重新核对后保存。${fail(error)}`);} catch {setError(modal,'案例已更新，暂时无法读取最新正文。请保留结论，稍后重新打开再写回。');modal.caseRevision = null;}}
        }
      });if (modal) {modal.revision = data.revision;modal.caseRevision = item?.revision;modal.product = script.product;const destination = $('[name="destination"]',modal.form);if (destination) destination.onchange = () => {modal.button = destination.value === 'existing' ? '保存为案例修订' : '创建案例';setBusy(modal,false);};}
    } catch (error) {toast(`暂时无法打开案例沉淀：${fail(error)}`,true);}
  }
  return {route,adoptCandidate,openManual,openCase,resumeTableResult,canResumeTableResult,modalOpen,hasUnsavedInput,beforeNavigate,captureListState,mountAdopted,mountTaskAdopted};
})();
