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
  const scriptLink = script => url(`#scripts/${encodeURIComponent(scriptId(script))}`,script.product);
  const sourceCase = script => script.case_id || script.source?.case_id || '';
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
  function scriptCard(script) {
    return `<a class="script-card" href="${esc(scriptLink(script))}"><div><span class="script-status ${esc(script.status || 'draft')}">${esc(labels[script.status] || script.status || '草稿')}</span><small>${esc(script.version ? `版本 ${script.version}` : scriptId(script))}</small></div><h3>${esc(script.title || '未命名脚本')}</h3><p>${esc(String(script.text || '').slice(0,170))}</p><small>${esc(script.direction_id || '未指定方向')} · ${esc(readableDate(script.updated_at || script.created_at))}${script.video_id ? ' · 已关联视频' : ''}</small></a>`;
  }
  async function route(keepScroll = false) {
    const hash = pageHash(), seq = ++workspace.sequence, tail = hash.replace(/^#scripts\/?/,'').split('?')[0];
    $('#breadcrumb').innerHTML = `工作空间 <span>/</span> 写稿 <span>/</span> ${tail ? '脚本详情' : '脚本版本'}`;
    $('#content').innerHTML = skeletonMarkup(2);
    try {
      if (tail) {
        const data = await api(`/api/scripts/${encodeURIComponent(decodeURIComponent(tail))}`);
        if (seq !== workspace.sequence || hash !== pageHash()) return;
        const script = data.script;
        if (currentProduct() && !window.WorkbenchShell.matchesProduct(script.product)) {location.hash = scriptLink(script);return;}
        workspace.loaded = data;renderDetail(data);
      } else {
        const data = await catalog(currentProduct());if (seq !== workspace.sequence || hash !== pageHash()) return;
        workspace.loaded = data;renderList(data);
      }
      if (!keepScroll) window.scrollTo(0,0);
    } catch (error) {if (seq !== workspace.sequence || hash !== pageHash()) return;$('#content').innerHTML = `<div class="empty"><h3>暂时无法读取脚本</h3><p>${esc(fail(error))}</p><button class="secondary" id="script-retry">重新读取</button></div>`;$('#script-retry').onclick = () => route(true);}
  }
  function renderList(data) {
    const scripts = data.scripts || [];
    $('#content').innerHTML = `<div class="page-heading"><div><span class="eyebrow">SCRIPT VERSIONS</span><h1>脚本版本</h1><p>采纳的正文与每次改稿分别保存，登记视频后连接销售表现和案例结论。</p></div><button class="primary" id="script-new">手动保存脚本</button></div><nav class="scripts-tabs" aria-label="写稿内容"><a href="${esc(url('#ai'))}">协作任务</a><a href="${esc(url('#scripts'))}" aria-current="page">脚本版本</a></nav><div class="script-list-filters"><label class="field"><span>状态</span><select id="script-status-filter"><option value="">全部状态</option>${Object.entries(labels).map(([key,value]) => `<option value="${key}">${value}</option>`).join('')}</select></label><label class="field"><span>搜索脚本</span><input id="script-query" type="search" placeholder="标题、正文或方向编号"></label></div><div id="script-list"></div>`;
    const show = () => {
      const status = $('#script-status-filter').value, query = $('#script-query').value.trim().toLocaleLowerCase();
      const visible = scripts.filter(script => (!status || script.status === status) && (!query || `${script.title} ${script.text} ${script.direction_id}`.toLocaleLowerCase().includes(query)));
      const groups = new Map();for (const script of visible) {const key = `${script.product} · ${script.direction_id || '独立脚本'}`;if (!groups.has(key)) groups.set(key,[]);groups.get(key).push(script);}
      $('#script-list').innerHTML = visible.length ? [...groups].map(([title,items]) => `<section class="script-list-group"><h2>${esc(title)} <small>${items.length} 个版本</small></h2><div class="script-grid">${items.map(scriptCard).join('')}</div></section>`).join('') : '<div class="empty"><h3>还没有符合条件的脚本版本</h3><p>可以从已完成的 AI 候选采纳，或手动保存一份正文。</p></div>';
    };
    $('#script-status-filter').onchange = show;$('#script-query').oninput = show;$('#script-new').onclick = () => openManual();show();
  }
  function renderDetail(data) {
    const script = data.script, id = scriptId(script), source = script.source || {};
    const performanceItem = {sales_data:data.sales_data || {status:script.video_id ? 'not_found' : 'missing_video_id'},current:{metrics:{video_id:script.video_id || ''}}};
    $('#content').innerHTML = `<a class="back-link" href="${esc(url('#scripts',script.product))}">${icon('back')}全部脚本版本</a><div class="page-heading"><div><span class="eyebrow">SCRIPT VERSION</span><h1>${esc(script.title || '未命名脚本')}</h1><p>${esc(script.product)} · ${esc(script.direction_id || '未指定方向')} · <span class="script-status ${esc(script.status || 'draft')}">${esc(labels[script.status] || script.status || '草稿')}</span></p></div><div class="scripts-start-actions"><button class="secondary" id="script-derive">改成新版本</button><button class="primary" id="script-case">${script.case_id ? '补充案例结论' : '沉淀为案例'}</button></div></div><div class="script-detail-grid"><section class="section-card script-body"><div class="section-title-row"><h2>采用正文</h2><button class="subtle" id="script-copy">${icon('copy')}复制正文</button></div><pre>${esc(script.text || '')}</pre>${script.translation_zh ? `<h2>中文对照</h2><pre>${esc(script.translation_zh)}</pre>` : ''}</section><aside><section class="section-card"><h2>拍摄与发布</h2><dl class="script-facts"><dt>脚本版本</dt><dd>${esc(id)}</dd><dt>视频 ID</dt><dd>${esc(script.video_id || '尚未登记')}</dd><dt>发布日期</dt><dd>${esc(script.published_on || '尚未登记')}</dd></dl><button class="secondary" id="script-release">登记进度与视频</button><p class="context-note">同一视频按视频 ID 匹配销售数据。拍摄不同版本时请分别登记。</p></section><section class="section-card"><h2>来源与版本</h2><p>${esc(readableDate(script.created_at))}</p>${source.task_id ? `<p><a href="${esc(url(`#ai/${encodeURIComponent(source.task_id)}`,script.product))}">查看来源 AI 任务</a></p>` : '<p>手动整理的正文</p>'}${sourceCase(script) ? `<p><a href="${esc(url(`#case/${encodeURIComponent(sourceCase(script))}`,script.product))}">查看${script.case_id ? '关联' : '参考'}案例</a></p>` : ''}${script.parent_id ? `<p><a href="${esc(url(`#scripts/${encodeURIComponent(script.parent_id)}`,script.product))}">查看上一版本</a></p>` : ''}${script.direction_id ? `<p><a href="${esc(url(`#directions/${encodeURIComponent(script.direction_id)}`,script.product))}">查看方向</a></p>` : ''}${script.note ? `<p>${esc(script.note)}</p>` : ''}</section></aside></div><section id="script-sales">${typeof renderPerformanceSection === 'function' ? renderPerformanceSection(performanceItem) : ''}</section>`;
    $('#script-copy').onclick = () => copyText(script.text || '','脚本正文已复制');
    $('#script-derive').onclick = () => openManual(script);
    $('#script-release').onclick = () => openStatus(script,data.revision);
    $('#script-case').onclick = () => openCase(script,script.case_id || '');
    if (source.case_id && source.case_id !== script.case_id) {
      $('#script-case').insertAdjacentHTML('afterend','<button class="secondary" id="script-write-source">写回参考案例</button>');
      $('#script-write-source').onclick = () => openCase(script,source.case_id);
    }
    const history = script.history || [];
    if (history.length) $('#script-sales').insertAdjacentHTML('beforebegin',`<details class="section-card script-history"><summary>查看进度记录（${history.length} 条）</summary><ol>${history.map(entry => `<li><strong>${esc(labels[entry.status] || entry.status)}</strong> · ${esc(readableDate(entry.at))}${entry.video_id ? ` · 视频 ${esc(entry.video_id)}` : ''}${entry.note ? `<p>${esc(entry.note)}</p>` : ''}</li>`).join('')}</ol></details>`);
  }
  async function openManual(parent = null) {
    const product = parent?.product || currentProduct();if (!product) {toast('请先选择一个具体产品。',true);return;}
    try {
      const [data,directions] = await Promise.all([catalog(product),api(`/api/direction-catalog?product=${encodeURIComponent(product)}`)]);
      const body = `<p class="context-note">${parent ? '将正文保存为新版本，上一版本完整保留。' : '保存准备采用的单条正文。'}</p>${field('title','脚本标题',parent?.title || '',{required:true})}<label class="field"><span>关联方向</span><select name="direction_id"><option value="">暂不指定</option>${(directions.directions || []).filter(item => item.status !== 'archived' || item.direction_id === parent?.direction_id).map(item => `<option value="${esc(item.direction_id)}" ${item.direction_id === parent?.direction_id ? 'selected' : ''}>${esc(item.direction_id)} · ${esc(item.main_difference)}</option>`).join('')}</select></label>${field('text','采用正文',parent?.text || '',{area:true,rows:10,required:true})}${field('translation_zh','中文对照',parent?.translation_zh || '',{area:true})}`;
      const modal = showModal(parent ? '保存新脚本版本' : '手动保存脚本',body,'保存脚本版本',async modal => {
        const values = Object.fromEntries(new FormData(modal.form));
        try {const result = await post(modal,'/api/scripts',{revision:modal.revision,script:{...values,product,confirmed:true,...(parent ? {parent_id:scriptId(parent)} : {})}});const saved = result.script;closeModal(true);location.hash = scriptLink(saved);toast('脚本版本已保存。');}
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
      const modal = showModal(writeBack ? '核对正文后写回案例' : '采纳为脚本版本',body,writeBack ? '核对并继续写回' : '核对并采纳',async modal => {
        const values = Object.fromEntries(new FormData(modal.form));
        try {
          const result = await post(modal,'/api/scripts',{revision:modal.revision,script:{...values,product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:modal.taskRevision}});
          const saved = result.script;closeModal(true);toast('候选已采纳为独立脚本版本。');
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
  function openStatus(script,revision) {
    const body = `<p class="context-note">${esc(script.title)} · ${esc(scriptId(script))}</p><label class="field"><span>当前状态</span><select name="status">${Object.entries(labels).map(([key,label]) => `<option value="${key}" ${script.status === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>${field('video_id','视频 ID',script.video_id || '')}${field('published_on','发布日期',script.published_on || '',{type:'date'})}${field('note','本次进度备注','',{area:true,rows:3})}`;
    const modal = showModal('登记进度与视频',body,'保存进度',async modal => {
      const values = Object.fromEntries(new FormData(modal.form));
      try {await post(modal,`/api/scripts/${encodeURIComponent(scriptId(script))}/status`,{revision:modal.revision,script_status:values});closeModal(true);await route(true);toast('脚本进度与视频关联已保存。');}
      catch (error) {await handleSaveError(modal,error);if (error.status === 409) {try {const fresh = await api(`/api/scripts/${encodeURIComponent(scriptId(script))}`);modal.revision = fresh.revision;setError(modal,`记录已更新，本次未保存。当前记录：${labels[fresh.script.status] || fresh.script.status}；视频 ${fresh.script.video_id || '未登记'}；发布日期 ${fresh.script.published_on || '未登记'}。你的输入仍保留，请核对后再保存。`);} catch {}}}
    });if (modal) {modal.revision = revision;modal.product = script.product;}
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
  return {route,adoptCandidate,openManual,openCase,modalOpen,hasUnsavedInput,beforeNavigate};
})();
