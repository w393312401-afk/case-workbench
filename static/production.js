'use strict';

// Production jobs: pick a voiced variant, keep a voice library and generate timed voiceovers.
window.CaseProduction = (() => {
  const statusLabels = {draft:'待配音',voiced:'已配音',timeline:'已建时间线'};
  const frameRates = ['23.976','24','25','29.97','30','50','59.94','60'];
  const editModes = {explain:'解释画面逻辑（默认）',sync:'音画同步',montage:'主题蒙太奇'};
  const editModels = {'claude-default':'Claude 客户端默认','claude-opus':'Claude Opus','claude-sonnet':'Claude Sonnet'};
  const editLabels = {running:'运行中',completed:'已完成',failed:'失败',cancelled:'已停止',interrupted:'已中断'};
  const today = () => {const date = new Date();return `${String(date.getFullYear()).slice(2)}${String(date.getMonth() + 1).padStart(2,'0')}${String(date.getDate()).padStart(2,'0')}`;};
  const sourceLabels = {tts:'配音自带时间',tts_normalized:'配音时间（规范化文本）',forced:'强制对齐','':'未取得'};
  const settingLabels = {stability:'稳定度',similarity_boost:'相似度',style:'风格',speed:'语速'};
  const ranges = {stability:[0,1,.05,.5],similarity_boost:[0,1,.05,.75],style:[0,1,.05,0],speed:[.7,1.2,.01,1]};
  const view = {sequence:0,modal:null,pending:new Map(),choice:{},showArchived:false};
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const currentProduct = () => window.WorkbenchShell?.product() || '';
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const fail = error => error?.message || '操作暂未完成';
  const definitive = error => Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
  const jobLink = job => url(`#production/${encodeURIComponent(job.id)}`,job.product);
  const seconds = value => value === null || value === undefined ? '—' : `${Number(value).toFixed(2)} 秒`;
  const voiceLabel = voice => [voice.note,voice.name].filter(Boolean).join(' · ') || voice.voice_id;
  const audioURL = (job,take,kind,download = false) => `/api/production/audio/${encodeURIComponent(job)}/${encodeURIComponent(take)}/${kind}${download ? '?download=1' : ''}`;
  const tabs = () => `<nav class="scripts-tabs" aria-label="写稿内容"><a href="${esc(url('#ai'))}">协作任务</a><a href="${esc(url('#scripts'))}">脚本版本</a><a href="${esc(url('#production'))}" aria-current="page">制作视频</a></nav>`;
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

  // A retried request keeps its id until the server gives a definite answer, so nothing is created twice.
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
      catch (error) {setError(dialog,definitive(error) ? fail(error) : `结果尚未确认：${fail(error)}。可以直接重试，同一请求不会重复创建。`);}
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

  async function route(keepScroll = false, refreshStatus = false) {
    const hash = pageHash(), seq = ++view.sequence, tail = hash.replace(/^#production\/?/,'').split('?')[0];
    if (refreshStatus) statusReads.forEach(entry => {entry.expires = 0;});
    if (view.modal && !view.modal.busy) closeModal(true);
    $('#breadcrumb').innerHTML = `工作空间 <span>/</span> 写稿 <span>/</span> ${tail ? '制作单' : '制作视频'}`;
    if (!keepScroll) $('#content').innerHTML = skeletonMarkup(2);
    try {
      if (tail) {
        const data = await api(`/api/production/jobs/${encodeURIComponent(decodeURIComponent(tail))}`);
        if (seq !== view.sequence || hash !== pageHash()) return;
        renderJob(data,seq);
      } else {
        const data = await api(`/api/production?product=${encodeURIComponent(currentProduct())}`);
        if (seq !== view.sequence || hash !== pageHash()) return;
        renderList(data);
      }
    } catch (error) {
      if (seq !== view.sequence || hash !== pageHash()) return;
      $('#content').innerHTML = `<div class="empty"><h3>暂时无法读取制作资料</h3><p>${esc(fail(error))}</p><button class="secondary" id="production-retry">重新读取</button></div>`;
      $('#production-retry').onclick = () => route(true);
    }
  }

  async function elevenStatus(node, force = false) {
    node.innerHTML = '<span>ElevenLabs：正在检查连接…</span>';
    try {
      const status = await readStatus('/api/production/eleven',30000,force);
      if (!node.isConnected) return;
      node.classList.toggle('problem',!status.ok);
      node.innerHTML = status.ok
        ? `<span>ElevenLabs 已连接${status.tier ? ` · ${esc(status.tier)}` : ''}${Number.isFinite(status.character_limit) ? ` · 本期已用 ${Number(status.character_count || 0).toLocaleString()} / ${Number(status.character_limit).toLocaleString()} 字符` : ''}</span><button type="button" class="subtle" data-recheck>重新检查</button>`
        : `<span>${esc(status.error)}</span><p>在 ElevenLabs 后台复制以 sk_ 开头的完整密钥，在 PowerShell 运行：<code>[Environment]::SetEnvironmentVariable('ELEVENLABS_API_KEY','你的密钥','User')</code>，然后点「重新检查」，不用重启工作台。</p><button type="button" class="subtle" data-recheck>重新检查</button>`;
    } catch (error) {if (!node.isConnected) return;node.classList.add('problem');node.innerHTML = `<span>暂时无法检查 ElevenLabs：${esc(fail(error))}</span><button type="button" class="subtle" data-recheck>重新检查</button>`;}
    const again = $('[data-recheck]',node);if (again) again.onclick = () => elevenStatus(node,true);
  }

  function renderList(data) {
    const product = currentProduct();
    const jobs = (data.jobs || []).filter(job => view.showArchived || !job.archived).sort((a,b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    const archivedCount = (data.jobs || []).filter(job => job.archived).length;
    const voices = data.voices || [];
    $('#content').innerHTML = `<div class="page-heading"><div><h1>制作视频</h1><p>选定变体后用 ElevenLabs 配音，每个意群都有配音时间，下一步交给达芬奇建时间线和剪辑。</p></div><button type="button" class="secondary" id="production-settings-open" aria-haspopup="dialog" aria-controls="production-settings">${icon('folder')}制作设置</button></div>${tabs()}<div class="production-eleven" id="production-eleven"></div><div class="production-main"><section><div class="section-title-row production-list-head"><h2>制作单 <small>${jobs.length} 张</small></h2>${archivedCount ? `<label class="production-toggle"><input type="checkbox" id="production-archived" ${view.showArchived ? 'checked' : ''}>显示已归档（${archivedCount}）</label>` : ''}</div>${jobs.length ? `<div class="script-rows">${jobs.map(job => `<a class="script-card production-card" href="${esc(jobLink(job))}"><span class="script-row-main"><strong>${esc(job.title)}</strong><small>${esc(job.direction_id || '未指定方向')} · ${job.lines.length} 个意群 · ${job.takes.length} 版配音${job.voicing ? ' · 正在生成配音' : ''}</small></span><span class="script-row-meta"><span>${esc(job.product)}</span><span>${esc(readableDate(job.updated_at))}</span></span><span class="script-status ${['voiced','timeline'].includes(job.status) ? 'published' : ''}">${esc(job.archived ? '已归档' : statusLabels[job.status] || job.status)}</span></a>`).join('')}</div>` : `<div class="empty"><h3>还没有制作单</h3><p>在「协作任务」的完成候选上点「制作视频」选一条变体，或在脚本版本详情里点「制作视频」。</p></div>`}</section></div><dialog id="production-settings" class="rev-drawer production-drawer" aria-labelledby="production-settings-title"><div class="rev-drawer-head"><h2 id="production-settings-title">制作设置</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭制作设置">${icon('close')}</button></div><div class="rev-drawer-body"><section class="production-setting"><h2>音色库</h2><p class="context-note">生成过的音色会自动留在这里；备注写清是谁的声音、适合什么稿。</p><ul class="voice-list">${voices.length ? voices.map(voice => `<li class="voice-row ${voice.disabled ? 'disabled' : ''}"><div><strong>${esc(voiceLabel(voice))}</strong>${product && data.defaults?.[window.WorkbenchShell.normalizeProduct(product)] === voice.voice_id ? '<span class="script-status published">默认</span>' : ''}${voice.disabled ? '<span class="script-status">已停用</span>' : ''}<code>${esc(voice.voice_id)}</code><small>使用 ${voice.use_count || 0} 次${voice.last_used_at ? ` · 最近 ${esc(readableDate(voice.last_used_at))}` : ''}</small></div><div class="voice-actions"><button type="button" class="subtle" data-voice-note="${esc(voice.voice_id)}">备注</button>${product && !voice.disabled ? `<button type="button" class="subtle" data-voice-default="${esc(voice.voice_id)}">设为默认</button>` : ''}<button type="button" class="subtle" data-voice-toggle="${esc(voice.voice_id)}">${voice.disabled ? '启用' : '停用'}</button></div></li>`).join('') : '<li class="context-note">还没有音色。</li>'}</ul><form id="voice-add" class="inline-form"><label class="field"><span>音色 ID</span><input name="voice_id" required pattern="[A-Za-z0-9]{10,64}" autocomplete="off"></label><label class="field"><span>备注</span><input name="note" maxlength="500" placeholder="例如：Zack 男声，适合测评稿"></label><button type="submit" class="secondary">核对并加入</button><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="production-setting"><h2>读法表${product ? ` · ${esc(product)}` : ''}</h2>${product ? `<p class="context-note">只改送去配音的文字，稿件原文不变。按整词、区分大小写替换。</p><ul class="reading-list">${(data.readings || []).map(item => `<li><span><code>${esc(item.written)}</code> 读作 <strong>${esc(item.spoken)}</strong></span><button type="button" class="subtle" data-reading-remove="${esc(item.id)}">删除</button></li>`).join('') || '<li class="context-note">还没有登记读法。</li>'}</ul><form id="reading-add" class="inline-form"><label class="field"><span>稿中写法</span><input name="written" required maxlength="100" placeholder="例如 202H"></label><label class="field"><span>配音读法</span><input name="spoken" required maxlength="200" placeholder="例如 two oh two H"></label><button type="submit" class="secondary">添加读法</button><p class="ai-error" data-error role="alert" hidden></p></form>` : '<p class="context-note">选择具体产品后维护读法。</p>'}</section>${resolveSettings(data,product)}</div></dialog>`;
    elevenStatus($('#production-eleven'));
    bindResolveSettings(data,product);
    // Saving a setting re-reads the page; the drawer stays open while you keep editing.
    const drawer = $('#production-settings');
    $('#production-settings-open').onclick = () => {view.settingsOpen = true;drawer.showModal();};
    drawer.addEventListener('click',event => {if (event.target === drawer || event.target.closest('[data-drawer-close]')) drawer.close();});
    drawer.addEventListener('close',() => {view.settingsOpen = false;});
    if (view.settingsOpen) drawer.showModal();
    const archived = $('#production-archived');if (archived) archived.onchange = () => {view.showArchived = archived.checked;renderList(data);};
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
  function editRun(job,edit) {
    const summary = edit.summary, report = edit.report || {}, live = edit.live || {};
    const facts = summary ? [`镜头 ${summary.shots} 个`,`配音 ${summary.voice_seconds} 秒`,`缺素材 ${summary.gaps.length} 处`,
      summary.uncovered.length ? `未覆盖 ${summary.uncovered.length} 段` : '画面全部覆盖',
      summary.long_shots ? `超过 ${summary.frame_limit} 帧的镜头 ${summary.long_shots} 个（最长 ${summary.max_shot} 帧）` : ''].filter(Boolean) : [];
    const warnings = summary ? [summary.voice_changed ? '配音轨和建时间线时不一致，请核对是否被改动。' : '',
      summary.group_markers !== summary.groups_expected ? `意群标记读回 ${summary.group_markers} 个，应有 ${summary.groups_expected} 个。` : ''].filter(Boolean) : [];
    if (edit.readback_error) warnings.push(`没能读回时间线：${edit.readback_error}`);
    return `<article class="edit-card ${esc(edit.status)}"><div class="take-head"><div><strong><span class="script-status ${edit.status === 'completed' ? 'published' : ''}">${esc(editLabels[edit.status] || edit.status)}</span> ${esc(edit.timeline_name)}</strong><small>${esc(editModels[edit.model] || edit.model)}${edit.effort ? ` · ${esc(edit.effort)}` : ''} · ${esc(editModes[edit.mode] || edit.mode)} · 开始 ${esc(readableDate(edit.started_at))}${edit.finished_at ? ` · 结束 ${esc(readableDate(edit.finished_at))}` : ''}</small></div>${edit.status === 'running' ? `<button type="button" class="secondary" data-cancel-edit="${esc(edit.id)}">停止</button>` : ''}</div>${edit.status === 'running' ? `<p class="production-busy">${esc(live.progress || '正在剪辑…')}</p>${live.latest ? `<pre class="edit-live">${esc(live.latest)}</pre>` : ''}` : ''}${facts.length ? `<p class="edit-facts">${facts.map(esc).join(' · ')}</p>` : ''}${warnings.length ? `<ul class="take-warnings">${warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}${summary?.gaps.length ? `<h3>缺素材</h3><ul class="edit-gaps">${summary.gaps.map(gap => `<li><strong>第 ${gap.seconds} 秒 · ${esc(gap.name)}</strong>${gap.note ? `<p>${esc(gap.note)}</p>` : ''}</li>`).join('')}</ul>` : ''}${summary?.uncovered.length ? `<h3>没有画面也没有缺素材标记的地方</h3><ul class="edit-gaps">${summary.uncovered.map(item => `<li>第 ${item.seconds} 秒起，共 ${item.end - item.start} 帧</li>`).join('')}</ul>` : ''}${(report.questions || []).length ? `<h3>待你确认</h3><ul class="edit-gaps">${report.questions.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}${edit.error ? `<p class="ai-error">${esc(edit.error)}</p>` : ''}${edit.output ? `<details><summary>Claude 的剪辑说明</summary><pre class="edit-output">${esc(edit.output)}</pre></details>` : ''}${edit.instruction ? `<p class="context-note">本次额外要求：${esc(edit.instruction)}</p>` : ''}</article>`;
  }
  function editSection(data,choice) {
    const job = data.job, timelines = [...(job.timelines || [])].reverse(), edits = [...(job.edits || [])].reverse();
    const running = edits.some(edit => edit.status === 'running');
    const form = !timelines.length ? '<p class="context-note">先建时间线，再让 Claude 按意群标记去素材里选画面、拼成视频。</p>'
      : `<form id="edit-form"><div class="form-row"><label class="field"><span>目标时间线</span><select name="timeline">${timelines.map(item => `<option value="${esc(item.unique_id)}" ${item.unique_id === choice.edit_timeline ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}</select></label><label class="field"><span>选画面模式</span><select name="mode">${Object.entries(editModes).map(([key,label]) => `<option value="${key}" ${key === (choice.edit_mode || 'explain') ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div><fieldset class="material-picker"><legend>素材时间线</legend><div data-materials>正在读取达芬奇里的时间线…</div></fieldset><label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="3" maxlength="4000" placeholder="例如：开头用切纸结果；只剪前 4 个意群">${esc(choice.edit_instruction || '')}</textarea></label><div class="form-row"><label class="field"><span>模型</span><select name="model">${Object.entries(editModels).map(([key,label]) => `<option value="${key}" ${key === (choice.edit_model || 'claude-default') ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label class="field"><span>思考强度</span><select name="effort"><option value="">客户端默认</option>${['high','xhigh','max'].map(value => `<option ${value === choice.edit_effort ? 'selected' : ''}>${value}</option>`).join('')}</select></label></div><p class="context-note">剪辑在后台运行，通常要 20–60 分钟，使用 Claude 订阅额度。期间请不要在达芬奇里操作；结束后工作台会自己读回时间线，列出缺素材和没有画面的位置。也可以「复制剪辑指令」，粘到 Claude Code 对话里边看边剪。</p><div class="take-actions"><button type="submit" class="primary" ${running || job.archived ? 'disabled' : ''}>开始 AI 剪辑</button><button type="button" class="secondary" data-copy-handoff>复制剪辑指令</button>${running ? '<span>已有剪辑在运行。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="edit-panel"><h2>AI 剪辑 <small>${edits.length} 次</small></h2>${form}<div id="edit-runs">${edits.map(edit => editRun(job,edit)).join('')}</div></section>`;
  }
  function bindEdit(data,choice,seq,request) {
    const job = data.job, form = $('#edit-form');
    const bindRuns = () => $$('[data-cancel-edit]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.cancelEdit)}/cancel`,{method:'POST',body:{}});toast('正在停止剪辑。');}
      catch (error) {toast(fail(error),true);button.disabled = false;}
    });
    bindRuns();
    if ((job.edits || []).some(edit => edit.status === 'running')) {
      const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
      const poll = async () => {
        if (!isCurrent()) return;
        if (document.hidden) {setTimeout(poll,5000);return;}
        try {
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (!isCurrent()) return;
          if (!(fresh.job.edits || []).some(edit => edit.status === 'running')) {await route(true);return;}
          $('#edit-runs').innerHTML = [...fresh.job.edits].reverse().map(edit => editRun(fresh.job,edit)).join('');bindRuns();
        } catch {}
        setTimeout(poll,5000);
      };
      setTimeout(poll,5000);
    }
    if (!form) return;
    const materials = () => $$('input[name="material"]:checked',form).map(input => input.value).concat((form.material_text?.value || '').split(/[,，]/).map(item => item.trim()).filter(Boolean));
    request.then(status => {
      if (!form.isConnected || seq !== view.sequence) return;
      const box = $('[data-materials]',form);if (!box) return;
      const saved = data.product_settings.material_timelines || [];
      if (status.ok && status.timelines?.length) {
        const own = new Set((job.timelines || []).map(item => item.name));
        const names = status.timelines.filter(name => !own.has(name));
        const chosen = choice.edit_materials || (saved.length ? saved : names.filter(name => name.includes('素材')));
        box.innerHTML = `<div class="material-options">${names.map(name => `<label><input type="checkbox" name="material" value="${esc(name)}" ${chosen.includes(name) ? 'checked' : ''}>${esc(name)}</label>`).join('')}</div>`;
      } else box.innerHTML = `<p class="context-note">暂时读不到达芬奇里的时间线列表，可以直接填写素材时间线名（逗号分隔）。</p><input name="material_text" value="${esc((choice.edit_materials || saved).join('，'))}">`;
    });
    const options = () => {
      const values = Object.fromEntries(new FormData(form));
      Object.assign(choice,{edit_timeline:values.timeline,edit_mode:values.mode,edit_instruction:values.instruction,edit_model:values.model,edit_effort:values.effort || null,edit_materials:materials()});
      return {timeline:values.timeline,mode:values.mode,instruction:values.instruction.trim(),materials:materials()};
    };
    $('[data-copy-handoff]',form).onclick = async event => {
      const button = event.currentTarget;button.disabled = true;setError(form,'');
      try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/handoff`,{method:'POST',body:options()});await copyText(result.prompt,'剪辑指令已复制，交接文件也已保存。');}
      catch (error) {setError(form,fail(error));}
      finally {button.disabled = false;}
    };
    form.onsubmit = async event => {
      event.preventDefault();setError(form,'');
      const body = {...options(),model:choice.edit_model,effort:choice.edit_effort};
      if (!body.materials.length) {setError(form,'请至少选一条素材时间线。');return;}
      const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在核对达芬奇并启动…';
      try {await send(`edit-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edit`,body);toast('AI 剪辑已开始，完成后这里会显示结果。');}
      catch (error) {setError(form,fail(error));submit.disabled = false;submit.textContent = '开始 AI 剪辑';return;}
      if (seq === view.sequence) await route(true);
    };
  }
  function timelineSection(data,choice,selected) {
    const job = data.job, timelines = [...(job.timelines || [])].reverse(), creators = data.creators || [], config = data.product_settings;
    const creator = choice.creator && creators.includes(choice.creator) ? choice.creator : timelines.find(item => creators.includes(item.creator))?.creator || creators[0] || '';
    const form = !selected ? '<p class="context-note">先选用一版配音，再建时间线。</p>'
      : !creators.length ? `<p class="context-note">还没有达人名，请到 <a href="${esc(url('#production',job.product))}">制作视频</a> 页的「达芬奇设置」里添加。</p>`
      : `<form id="timeline-form"><div class="form-row"><label class="field"><span>达人</span><select name="creator">${creators.map(name => `<option ${name === creator ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select></label><div></div></div><p class="context-note">将在项目「${esc(config.project)}」里新建 <code data-timeline-name>${esc(creator)}${esc(data.today)}-${esc(config.code)}-序号</code>（序号按项目里已有的时间线顺延）。配音放在单独的「配音」轨（音频 2），每个意群一个蓝色范围标记，备注里写着口播、中文和画面要求；视频轨留空，下一步交给剪辑。</p><div class="take-actions"><button type="submit" class="primary" ${job.archived ? 'disabled' : ''}>在达芬奇建时间线</button>${timelines.length && timelines[0].take_id !== job.selected_take ? '<span>当前选用的配音和最近一条时间线用的不同，会另建一条新时间线，旧的保留。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="timeline-panel"><h2>达芬奇时间线 <small>${timelines.length} 条</small></h2><p class="production-resolve" data-resolve-status>正在检查达芬奇…</p>${form}${timelines.map(item => `<article class="timeline-card"><div><strong>${esc(item.name)}</strong><small>${esc(item.creator)} · ${item.markers} 个意群标记 · ${(item.frames / Number(item.fps)).toFixed(1)} 秒 · 配音 ${esc(item.take_id)}${item.take_id === job.selected_take ? '（当前选用）' : ''} · ${esc(readableDate(item.built_at))}</small>${(item.warnings || []).length ? `<ul class="take-warnings">${item.warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}</div><button type="button" class="secondary" data-open-timeline="${esc(item.unique_id)}">在达芬奇打开</button></article>`).join('')}</section>`;
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

  function renderJob(data,seq) {
    const job = data.job, takes = [...job.takes].reverse(), selected = job.takes.find(take => take.id === job.selected_take);
    const voices = (data.voices || []).filter(voice => !voice.disabled), models = data.models || [];
    const choice = view.choice[job.id] ||= {};
    const lastTake = job.takes[job.takes.length - 1];
    const voiceId = choice.voice_id || data.defaults?.[job.product] || lastTake?.voice_id || voices[0]?.voice_id || '__new';
    const modelId = choice.model_id || voices.find(voice => voice.voice_id === voiceId)?.model_id || data.default_model;
    const voiceName = id => {const voice = (data.voices || []).find(item => item.voice_id === id);return voice ? voiceLabel(voice) : id;};
    const modelName = id => models.find(item => item.id === id)?.name || id;
    $('#content').innerHTML = `<a class="back-link" href="${esc(url('#production',job.product))}">${icon('back')}全部制作单</a><div class="page-heading"><div><span class="eyebrow">PRODUCTION JOB</span><h1>${esc(job.title)}</h1><p>${esc(job.product)} · ${esc(job.direction_id || '未指定方向')}${job.variant_label ? ` · ${esc(job.variant_label)}` : ''} · <span class="script-status ${['voiced','timeline'].includes(job.status) ? 'published' : ''}">${esc(job.archived ? '已归档' : statusLabels[job.status] || job.status)}</span>${data.script ? ` · <a href="${esc(url(`#scripts/${encodeURIComponent(data.script.id)}`,job.product))}">脚本版本 ${esc(data.script.version)}</a>` : ' · 脚本版本已不可读'}</p></div><div class="scripts-start-actions"><button class="secondary" id="job-archive">${job.archived ? '恢复制作单' : '归档'}</button></div></div>${problemsMarkup(job.problems)}<section class="section-card"><div class="section-title-row"><h2>意群表 <small>${job.lines.length} 个意群</small></h2><button type="button" class="subtle" id="job-lines-edit">${icon('edit')}编辑画面要求</button></div>${linesTable(job.lines,selected ? selected.groups : null)}${selected ? `<p class="context-note">配音时间来自已选用的配音（${esc(sourceLabels[selected.alignment_source] || selected.alignment_source)}），单位秒；下一步建时间线时按这些时间打意群标记。</p>` : '<p class="context-note">选用一版配音后，这里显示每个意群在配音里的起止时间。</p>'}</section><section class="section-card" id="voice-panel"><h2>配音</h2>${job.voicing ? '<p class="production-busy">正在生成配音，完成后自动刷新…</p>' : ''}<form id="take-form" class="take-form"><div class="form-row"><label class="field"><span>音色</span><select name="voice">${voices.map(voice => `<option value="${esc(voice.voice_id)}" ${voice.voice_id === voiceId ? 'selected' : ''}>${esc(voiceLabel(voice))}${data.defaults?.[job.product] === voice.voice_id ? '（默认）' : ''}</option>`).join('')}<option value="__new" ${voiceId === '__new' ? 'selected' : ''}>新音色 ID…</option></select></label><label class="field"><span>模型</span><select name="model_id">${models.map(model => `<option value="${esc(model.id)}" ${model.id === modelId ? 'selected' : ''}>${esc(model.name)}</option>`).join('')}</select></label></div><div class="form-row" data-new-voice ${voiceId === '__new' ? '' : 'hidden'}><label class="field"><span>新音色 ID</span><input name="new_voice_id" pattern="[A-Za-z0-9]{10,64}" autocomplete="off" value="${esc(choice.new_voice_id || '')}"></label><label class="field"><span>备注（存入音色库）</span><input name="new_note" maxlength="500" value="${esc(choice.new_note || '')}"></label></div><div class="take-settings" data-settings></div><details class="take-text"><summary>送去配音的文字（${data.tts.characters} 字符${data.tts.substitutions ? `，已套用读法 ${data.tts.substitutions} 处` : ''}）</summary><pre>${esc(data.tts.text)}</pre></details><div class="take-actions"><button class="primary" type="submit" ${job.voicing || job.archived ? 'disabled' : ''}>生成配音</button><span>每次生成按字数消耗 ElevenLabs 额度（本条约 ${data.tts.characters} 字符），旧版本都会保留。</span></div><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="section-card"><h2>配音版本 <small>${takes.length} 版</small></h2>${takes.length ? takes.map(take => `<article class="take-card ${take.id === job.selected_take ? 'selected' : ''}"><div class="take-head"><div><strong>${esc(voiceName(take.voice_id))}</strong><small>${esc(modelName(take.model_id))} · ${Object.keys(settingLabels).filter(key => key in (take.settings || {})).map(key => `${settingLabels[key]} ${take.settings[key]}`).join(' · ')} · ${esc(seconds(take.duration))} · ${esc(readableDate(take.created_at))}</small></div>${take.id === job.selected_take ? '<span class="script-status published">已选用</span>' : `<button type="button" class="secondary" data-select-take="${esc(take.id)}" ${take.groups?.every(group => group.start !== null) ? '' : 'disabled title="没有完整的意群时间"'}>选用这一版</button>`}</div><audio controls preload="none" src="${esc(audioURL(job.id,take.id,'mp3'))}"></audio>${(take.warnings || []).length ? `<ul class="take-warnings">${take.warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}<details><summary>意群时间（${esc(sourceLabels[take.alignment_source] || take.alignment_source)}）</summary><table class="production-table compact"><thead><tr><th>#</th><th>开始</th><th>结束</th><th>对上的字</th></tr></thead><tbody>${(take.groups || []).map(group => `<tr><td>${group.number}</td><td>${esc(seconds(group.start))}</td><td>${esc(seconds(group.end))}</td><td>${Math.round((group.coverage || 0) * 100)}%</td></tr>`).join('')}</tbody></table></details><div class="take-links">${take.wav ? `<a href="${esc(audioURL(job.id,take.id,'wav',true))}">下载 WAV</a>` : ''}<a href="${esc(audioURL(job.id,take.id,'mp3',true))}">下载 MP3</a><small>${esc(take.id)}</small></div></article>`).join('') : '<p class="context-note">还没有配音。</p>'}</section>${timelineSection(data,choice,selected)}${editSection(data,choice)}<details class="section-card production-history"><summary>制作记录（${job.history.length} 条）</summary><ol>${[...job.history].reverse().map(entry => `<li>${esc(readableDate(entry.at))} · ${esc(entry.note || entry.event)}</li>`).join('')}</ol></details>`;
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
        setError(form,definitive(error) ? fail(error) : `结果尚未确认：${fail(error)}。稍后刷新查看；直接重试会沿用同一请求，不会重复扣额度。`);
        submit.disabled = false;submit.textContent = '生成配音';return;
      }
      if (seq === view.sequence) await route(true);
    };
    $$('[data-select-take]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {await send(`select-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/select`,{revision:data.revision,take_id:button.dataset.selectTake});toast('已选用这一版配音。');await route(true);}
      catch (error) {toast(fail(error),true);button.disabled = false;if (error.status === 409) await route(true);}
    });
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
      const poll = () => {
        if (seq !== view.sequence || pageHash().split('?')[0] !== `#production/${encodeURIComponent(job.id)}`) return;
        if (document.hidden) {setTimeout(poll,3000);return;}
        void route(true);
      };
      setTimeout(poll,3000);
    }
  }

  async function fromCandidate(task,round) {
    const product = task.product || task.context?.product || currentProduct(), direction = task.context?.direction_id || '';
    if (!product) {toast('请先选择一个具体产品。',true);return;}
    let data;
    try {data = await api(`/api/production/variants?task_id=${encodeURIComponent(task.task_id)}&round_id=${encodeURIComponent(round.round_id)}`);}
    catch (error) {toast(error.status === 409 ? `${fail(error)} 旧格式候选可以先「采纳为脚本版本」（一行一个意群），再在脚本版本里点「制作视频」。` : `暂时无法拆分变体：${fail(error)}`,true);return;}
    const variants = data.variants, defaultTitle = variant => `${direction ? direction + ' ' : ''}${variant.label}`;
    const modal = openModal('选择要制作的变体',`<p class="context-note">口播按行配音，每行是一个意群。采纳后正文固定为脚本版本；改稿请另存新版本再制作。</p><div class="variant-options">${variants.map((variant,position) => `<label class="variant-option"><input type="radio" name="variant" value="${variant.index}" ${position === 0 ? 'checked' : ''}><span><strong>${esc(variant.label)}</strong><small>${variant.lines.length} 个意群${variant.problems.length ? ` · ${variant.problems.length} 处需核对` : ''}</small></span></label>`).join('')}</div><label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(defaultTitle(variants[0]))}"></label><div data-variant-preview></div>`,'采纳并创建制作单',async modal => {
      const chosen = variants.find(item => String(item.index) === new FormData(modal.form).get('variant'));
      const title = new FormData(modal.form).get('title').trim();
      const chinese = chosen.lines.every(line => line.zh) ? chosen.lines.map(line => line.zh).join('\n') : '';
      modal.done ||= {};
      if (modal.variant !== chosen.index) {modal.done = {};modal.requests = {};modal.variant = chosen.index;}
      const adopted = await once(modal,'script','/api/scripts',async () => ({revision:(await api(`/api/creation?product=${encodeURIComponent(product)}`)).revision,script:{title,text:chosen.lines.map(line => line.spoken).join('\n'),translation_zh:chinese,product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:data.task_revision}}));
      let created;
      try {created = await once(modal,'job','/api/production/jobs',async () => ({revision:(await api('/api/production')).revision,job:{script_id:adopted.script.id,variant_index:chosen.index}}));}
      catch (error) {if (definitive(error)) {closeModal(true);toast(`脚本已采纳，但制作单没有创建：${fail(error)}。可在脚本版本里再点「制作视频」。`,true);location.hash = url(`#scripts/${encodeURIComponent(adopted.script.id)}`,product);return;}throw error;}
      closeModal(true);toast('已采纳为脚本版本，并创建制作单。');location.hash = jobLink(created.job);
    });
    const preview = () => {
      const chosen = variants.find(item => String(item.index) === new FormData(modal.form).get('variant'));
      $('[data-variant-preview]',modal.dialog).innerHTML = problemsMarkup(chosen.problems) + linesTable(chosen.lines);
    };
    let titleEdited = false;
    modal.form.title.oninput = () => {titleEdited = true;};
    $$('input[name="variant"]',modal.dialog).forEach(input => input.onchange = () => {
      const chosen = variants.find(item => String(item.index) === input.value);
      if (!titleEdited) modal.form.title.value = defaultTitle(chosen);
      preview();
    });
    preview();
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

  return {route,fromCandidate,fromScript,modalOpen};
})();
