'use strict';

// Upload the selected timeline before the TikTok post exists. No post ID is required.
window.NotionUpload = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const url = id => `/api/production/jobs/${encodeURIComponent(id)}/notion`;
  const link = receipt => receipt?.page_id && /^[a-f0-9-]{32,36}$/i.test(receipt.page_id) ? `https://www.notion.so/${receipt.page_id.replaceAll('-','')}` : '';
  // Option text: the headline the post uses, then its Chinese reading for the eye only.
  const titleLabel = (title,zh) => title.split('#')[0].trim() + (zh ? `｜${zh}` : '');
  // Missing readings are made once and saved (shared with 成片与发布); uploading never waits for them.
  async function fillTitleChinese(dialog,job,api,data) {
    const titles = data.copy?.titles || [];
    if (!titles.length || data.copy.titles_zh?.every(Boolean)) return;
    let chinese = [];
    try {chinese = (await api(`/api/production/jobs/${encodeURIComponent(job.id)}/publication/title-zh`,{method:'POST',body:{}})).titles_zh || [];} catch {return;}
    if (!dialog.isConnected) return;
    data.copy.titles_zh = chinese;
    dialog.querySelectorAll('[data-notion-title-choice] option').forEach(option => {option.textContent = titleLabel(titles[Number(option.value)],chinese[Number(option.value)]);});
    const single = dialog.querySelector('[data-notion-title-zh]');
    if (single && chinese[0]) single.textContent = '中文：' + chinese[0];
  }
  const markup = uid => `<div class="notion-upload" data-notion-upload="${esc(uid)}"><button type="button" class="secondary compact" data-notion-open>上传到 Notion</button><span data-notion-status role="status"></span><a data-notion-link target="_blank" rel="noopener noreferrer" hidden>查看 Notion</a></div>`;
  function render(root,data) {
    const receipt = data.receipt, busy = receipt?.status === 'running';
    const button = root.querySelector('[data-notion-open]'), note = root.querySelector('[data-notion-status]'), anchor = root.querySelector('[data-notion-link]');
    button.textContent = busy ? '查看上传进度' : receipt?.status === 'completed' ? '更新到 Notion' : '上传到 Notion';
    button.disabled = Boolean(data.blocked_reason);
    button.title = data.blocked_reason || '上传所选时间线的成片、标题、中英文案和钩子屏显';
    note.textContent = busy ? receipt.progress : receipt?.status === 'completed' ? receipt.warning || '已上传' : receipt?.error || '';
    note.className = receipt?.status === 'failed' || receipt?.warning ? 'ai-error' : 'context-note';
    const href = link(receipt);
    anchor.hidden = !href;if (href) anchor.href = href;
  }
  function open(data,{job,api,host,refresh}) {
    const old = document.querySelector('dialog.notion-upload-dialog');
    if (old) {old.close();old.remove();}
    const dialog = document.createElement('dialog');
    dialog.className = 'script-editor-dialog production-dialog notion-upload-dialog';
    const receipt = data.receipt, active = receipt?.status === 'running';
    const titles = data.copy?.titles || [], timeline = (job.timelines || []).find(row => row.unique_id === data.timeline_uid);
    const pickedDay = host.querySelector('[data-calendar-plan] input[type="date"]')?.value;
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>${esc(receipt?.page_id ? '更新到 Notion' : '上传到 Notion')}</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div>
      <div class="script-editor-body notion-upload-fields"><p class="context-note">${esc(data.name)} → ${esc(data.destination)}。成片存入「文件和媒体」，正文放中英文案和钩子屏显。</p>
      <div class="notion-upload-connection"><span data-connection-status role="status">正在检查 Notion 连接…</span><button type="button" class="secondary compact" data-notion-connect hidden>连接 Notion</button><button type="button" class="secondary compact" data-notion-recheck hidden>重新检查</button><a data-notion-authorize target="_blank" rel="noopener noreferrer" hidden>前往 Notion 授权</a></div>
      <label class="field"><span>本地成片完整路径</span><input name="file_path" required maxlength="2000" value="${esc(data.file_path)}" placeholder="C:\\Videos\\最终成片.mp4"></label>${data.file_path ? '' : '<p class="context-note">还没找到这条时间线导出的成片：先在「视频预览」里导出，或填本地文件的完整路径。</p>'}
      <p class="context-note">超过 20 MiB 会生成上传版，本地高清原件保留。</p>
      <div class="form-row"><label class="field"><span>发布日期</span><input name="published_on" type="date" required value="${esc(pickedDay || data.published_on)}"></label><label class="field"><span>发布账号</span><input name="account" required maxlength="200" value="${esc(data.account)}" placeholder="Notion 账号列表里的用户名"></label></div>
      ${titles.length > 1 ? `<label class="field"><span>选择发布标题</span><select data-notion-title-choice>${titles.map((title,index) => `<option value="${index}">${esc(titleLabel(title,data.copy?.titles_zh?.[index]))}</option>`).join('')}</select></label>` : titles.length ? `<p class="context-note" data-notion-title-zh>${esc(data.copy?.titles_zh?.[0] ? '中文：' + data.copy.titles_zh[0] : '')}</p>` : ''}
      <label class="field"><span>标题＋tags</span><textarea name="title" rows="3" required maxlength="2000">${esc(receipt?.title || titles[0] || '')}</textarea></label>
      <div class="form-row"><label class="field"><span>钩子屏显</span><textarea name="screen_en" rows="2" maxlength="300">${esc(timeline?.hook_overlay?.text || data.copy?.screen_en || '')}</textarea></label><label class="field"><span>屏显中文</span><textarea name="screen_zh" rows="2" maxlength="300">${esc(data.copy?.screen_zh || '')}</textarea></label></div>
      <details><summary>查看上传的中英文案</summary><h4>英文文案</h4><pre>${esc(data.copy?.en || '')}</pre><h4>中文文案</h4><pre>${esc(data.copy?.zh || '尚未补充中文文案')}</pre></details>
      <p data-notion-progress role="status">${esc(active ? receipt.progress : '')}</p><p class="ai-error" data-notion-error role="alert" hidden></p>
      <a data-notion-result target="_blank" rel="noopener noreferrer" ${link(receipt) ? `href="${esc(link(receipt))}"` : 'hidden'}>查看 Notion 页面</a></div>
      <div class="dialog-footer"><span>${active ? '关闭窗口后仍会继续上传。' : ''}</span><div><button type="button" class="secondary" data-close>关闭</button><button type="submit" class="primary" data-notion-submit ${active || data.blocked_reason || !data.configured ? 'disabled' : ''}>${receipt?.page_id ? '更新到 Notion' : '上传到 Notion'}</button></div></div></form>`;
    document.body.append(dialog);dialog.showModal();
    const form = dialog.querySelector('form'), submit = dialog.querySelector('[data-notion-submit]'), error = dialog.querySelector('[data-notion-error]'), progress = dialog.querySelector('[data-notion-progress]');
    let busy = active, connected = false, request = null, connectionTimer = null, pollTimer = null;
    const updateSubmit = () => {submit.disabled = busy || !connected || Boolean(data.blocked_reason) || !data.configured;};
    updateSubmit();
    const close = () => {clearTimeout(connectionTimer);clearTimeout(pollTimer);dialog.close();dialog.remove();};
    dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    dialog.querySelector('[data-notion-title-choice]')?.addEventListener('change',event => {form.elements.title.value = titles[Number(event.target.value)] || '';});
    const fail = problem => {error.textContent = problem.message || '上传未完成，请重试。';error.hidden = false;};
    const watch = async () => {
      if (!dialog.isConnected) return;
      try {
        const latest = await api(`${url(job.id)}?timeline=${encodeURIComponent(data.timeline_uid)}`);
        refresh(latest);
        const current = latest.receipt;
        progress.textContent = current?.progress || '';
        if (current?.status === 'running') {pollTimer = setTimeout(watch,1500);return;}
        busy = false;updateSubmit();submit.textContent = current?.page_id ? '更新到 Notion' : '重试上传';
        form.querySelectorAll('input,textarea,select').forEach(node => node.disabled = false);
        if (current?.status === 'completed') {
          request = null;const result = dialog.querySelector('[data-notion-result]');result.href = link(current);result.hidden = false;
          progress.textContent = current.warning || '已上传到 Notion。';
          if (typeof toast === 'function') toast('已上传到 Notion。');
          window.VideoPublication?.markNotionUploaded?.(host,data.timeline_uid);
          // The selected timeline's own marker changes, preserving any typed post ID.
          if (host.querySelector('[data-notion-upload]')?.dataset.notionUpload === data.timeline_uid) {
            const mark = host.querySelector('[data-calendar-toggle]');
            if (mark) {mark.textContent = '已上传';mark.classList.add('is-uploaded');mark.dataset.calendarToggle = '0';mark.setAttribute('aria-label','改回未上传');}
            const hidden = host.querySelector('[name="calendar_uploaded"]');if (hidden) hidden.value = '1';
            if (window.VideoPublication && host.querySelector('[data-calendar-plan]')) {
              void window.VideoPublication.bindCalendarPlan(host,data.content_id,api,{form:host.querySelector('.video-publication-form')});
            }
          }
        } else {
          fail(new Error(current?.error || '上传中断，可重试恢复。'));
          // A failed upload may have cleared the remembered connection; show the real state again.
          if (/连接|授权/.test(current?.error || '')) void checkConnection();
        }
      } catch (problem) {fail(problem);pollTimer = setTimeout(watch,3000);}
    };
    const checkConnection = async (refresh = false) => {
      if (!dialog.isConnected) return;
      const note = dialog.querySelector('[data-connection-status]'), connect = dialog.querySelector('[data-notion-connect]'), authorize = dialog.querySelector('[data-notion-authorize]'), recheck = dialog.querySelector('[data-notion-recheck]');
      if (refresh) {note.textContent = '正在检查 Notion 连接…';recheck.hidden = true;}
      try {
        // The workbench remembers a confirmed connection, so this normally answers at once.
        const state = await api(refresh ? '/api/notion/connection?refresh=1' : '/api/notion/connection');
        connected = state.status === 'connected';updateSubmit();
        recheck.hidden = !connected;
        note.textContent = state.status === 'connected' ? 'Notion 已连接' : state.error || '请连接 Notion，并允许访问发布日历。';
        connect.hidden = state.status === 'connected';connect.disabled = false;
        if (state.authorization_url && /^https:\/\//.test(state.authorization_url)) {authorize.href = state.authorization_url;authorize.hidden = false;}
        else authorize.hidden = true;
        if (state.status === 'waiting' || state.status === 'preparing') connectionTimer = setTimeout(() => checkConnection(),2000);
      } catch (problem) {connected = false;updateSubmit();note.textContent = problem.message || '连接检查未完成。';connect.hidden = false;connect.disabled = false;}
    };
    dialog.querySelector('[data-notion-recheck]').onclick = () => void checkConnection(true);
    dialog.querySelector('[data-notion-connect]').onclick = async event => {
      event.currentTarget.disabled = true;error.hidden = true;
      try {const state = await api('/api/notion/connect',{method:'POST',body:{}});if (state.error) throw new Error(state.error);await checkConnection();}
      catch (problem) {fail(problem);event.currentTarget.disabled = false;}
    };
    if (!data.configured) fail(new Error('请先在本机配置 Notion 发布日历地址。'));
    if (data.blocked_reason) fail(new Error(data.blocked_reason));
    void checkConnection();
    void fillTitleChinese(dialog,job,api,data);
    if (active) {form.querySelectorAll('input,textarea,select').forEach(node => node.disabled = true);void watch();}
    form.onsubmit = async event => {
      event.preventDefault();if (busy || !connected) return;
      error.hidden = true;busy = true;submit.disabled = true;submit.textContent = '正在上传…';
      const body = {...Object.fromEntries(new FormData(form)),timeline_uid:data.timeline_uid};
      const signature = JSON.stringify(body);
      if (!request || request.signature !== signature) request = {signature,body:{...body,request_id:crypto.randomUUID()}};
      form.querySelectorAll('input,textarea,select').forEach(node => node.disabled = true);
      try {const started = await api(url(job.id),{method:'POST',body:request.body});refresh({...data,receipt:started});void watch();}
      catch (problem) {busy = false;updateSubmit();submit.textContent = '重试上传';form.querySelectorAll('input,textarea,select').forEach(node => node.disabled = false);fail(problem);}
    };
  }
  async function bind(host,{job,api,uid} = {}) {
    const root = host.querySelector('[data-notion-upload]');if (!root || !uid) return;
    let data, timer;
    const refresh = latest => {
      data = latest;clearTimeout(timer);
      if (root.isConnected) {render(root,latest);if (latest.receipt?.status === 'running') timer = setTimeout(load,2000);}
    };
    const load = async () => {
      if (!root.isConnected) {clearTimeout(timer);return;}
      try {refresh(await api(`${url(job.id)}?timeline=${encodeURIComponent(uid)}`));}
      catch (problem) {root.querySelector('[data-notion-status]').textContent = problem.message || 'Notion 状态暂时无法读取。';}
    };
    root.querySelector('[data-notion-open]').onclick = async () => {
      // Read again on every open: a preview exported after the panel was shown is the file to upload.
      try {await load();if (data) open(data,{job,api,host,refresh});}
      catch (problem) {if (typeof toast === 'function') toast(problem.message,true);}
    };
    await load();
  }
  return {markup,bind};
})();
