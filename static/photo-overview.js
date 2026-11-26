'use strict';

// 内容进程「图文」标签：每条已上传或已登记的视频一行，一眼看清哪些还没决定要不要做图文。
// 状态由图文组是否存在推出；只有「要做 / 不做」存成决定记录（见 photo_carousel.py）。
window.PhotoOverview = (() => {
  const states = {
    pending:{label:'待决定',tone:'warning'},
    want:{label:'要做',tone:'active'},
    doing:{label:'做中',tone:'active'},
    exported:{label:'待上传',tone:'warning'},
    uploaded:{label:'已上传',tone:'success'},
    finished:{label:'已完结',tone:'success'},
    skip:{label:'不做',tone:'muted'}
  };
  // Tabs: 要处理 (every open state, the default), each open state that has rows, and 已结束 (上传 / 完结 / 不做 together).
  const open = ['pending','want','doing','exported'], closed = ['uploaded','finished','skip'];
  const staleDays = 7;
  const ui = {only:'',selected:new Set(),recent:new Set(),day:'',poll:0,market:'de',autoExport:true};
  const keyOf = row => row.job_id + '|' + row.timeline_uid;
  // The title opens this timeline's 图文 on its order page (production.js reads photo=).
  const photoURL = row => {const hash = `#production/${encodeURIComponent(row.job_id)}?step=cut&photo=${encodeURIComponent(row.timeline_uid)}`;return window.WorkbenchShell?.url?.(hash,row.product) || hash;};
  const day = value => String(value || '').slice(5,10);
  const finishedDay = value => {const date = new Date(value);return Number.isNaN(date.getTime()) ? '' : `${String(date.getMonth() + 1).padStart(2,'0')}/${String(date.getDate()).padStart(2,'0')}`;};
  const stale = row => Date.now() - Date.parse(row.since || '') > staleDays * 86400000;
  const inTab = (row,tab) => tab === 'done' ? closed.includes(row.state) : tab ? row.state === tab : open.includes(row.state);
  // A row just changed stays in the open tab until the next visit, so the undo is still in reach.
  const visible = row => inTab(row,ui.only) || (!ui.only && ui.recent.has(keyOf(row)));
  // 外语版（2026-11-24）：有已定稿版本的行也能勾，批量改成德语等外语，评论贴纸不带。
  const tongues = {de:'德语',uk:'英式英语',es:'西语'};
  const translating = row => ['queued','running'].includes(row.translating?.status);
  const selectable = row => (row.state === 'pending' && !row.importing) || row.state === 'exported' || !!row.translatable;
  const foreignNote = row => {
    const notes = (row.foreign || []).map(item => `${tongues[item.market] || item.label}版 ${item.keys.join('、')}${item.done === item.keys.length ? ' 已导出' : ` 已写好 ${item.keys.length - item.done} 版待定稿`}`);
    const task = row.translating, tongue = tongues[task?.market] || '外语';
    if (task?.status === 'queued') notes.push(`排队出${tongue}版`);
    else if (task?.status === 'running') notes.push(task.progress || `正在出${tongue}版`);
    else if (task?.status === 'failed') notes.push(`${tongue}版没出成：${task.error || '请再试一次'}`);
    return notes;
  };
  // 按天导出无字版: the day a video went up, in local time (a bare date stays as it is).
  const localDay = value => {
    const text = String(value || '');if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    const date = new Date(text);if (Number.isNaN(date.getTime())) return '';
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
  };
  const weekday = text => '日一二三四五六'[new Date(text + 'T12:00').getDay()];
  const exportable = row => (row.state === 'pending' || row.state === 'want') && !row.importing;
  function days(rows) {
    const found = new Map();
    for (const row of rows) {const key = exportable(row) && localDay(row.since);if (key) found.set(key,[...(found.get(key) || []),row]);}
    return [...found.entries()].sort((a,b) => b[0].localeCompare(a[0]));
  }
  const dots = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg>';

  // One quiet button per row; permanent delete waits in the row's ··· menu and asks first.
  function actions(row) {
    const k = esc(keyOf(row)), name = esc(row.timeline_name || row.job_title || '');
    const go = (attr,label,cls = 'secondary') => `<button type="button" class="${cls} compact" ${attr}="${k}">${label}</button>`;
    const menu = `<details class="ai-menu po-menu"><summary class="icon-button" aria-label="更多操作：${name}">${dots}</summary><div role="menu"><button type="button" role="menuitem" class="po-menu-danger" data-po-delete="${k}">永久删除图文…</button></div></details>`;
    if (row.importing) return go('data-po-open','查看进度');
    if (row.state === 'pending') return go('data-po-want','做') + go('data-po-skip','不做','subtle');
    if (row.state === 'want') return go('data-po-open','开始做') + go('data-po-finish','完结','subtle') + go('data-po-clear','撤销','subtle');
    if (row.state === 'doing') return go('data-po-open','继续做') + go('data-po-finish','完结','subtle') + menu;
    if (row.state === 'exported') return go('data-po-open','上传 Notion') + go('data-po-finish','完结','subtle') + menu;
    if (row.state === 'finished') return go('data-po-open','查看') + go('data-po-reopen','撤销','subtle') + menu;
    if (row.state === 'uploaded') return go('data-po-open','查看') + menu;
    return go('data-po-want','改成要做') + go('data-po-clear','撤销','subtle');
  }

  // Each timeline is one row; its script name sits beside it instead of heading a group of one.
  function rowMarkup(row) {
    const info = row.importing ? {label:'导出无字版',tone:'active'} : states[row.state], k = keyOf(row), old = row.state === 'exported' && stale(row);
    const select = selectable(row) ? `<input type="checkbox" class="flow-select" data-po-select="${esc(k)}" aria-label="选择 ${esc(row.timeline_name || row.job_title)}" ${ui.selected.has(k) ? 'checked' : ''}>` : '<span aria-hidden="true"></span>';
    const meta = [row.uploaded ? '视频已上传 Notion' : '视频已登记发布', row.since ? day(row.since) : '', row.notion_works?.length ? `图文 ${row.notion_works.join('、')} 版已上传` : '', row.reason ? `原因：${row.reason}` : '',row.importing ? '正在从达芬奇导出无字版，导完自动抽帧' : '',!row.importing && row.import_error && exportable(row) ? `无字版导出失败：${row.import_error}` : '',row.state === 'finished' && row.finished_at ? `${finishedDay(row.finished_at)} 手动完结` : '',...foreignNote(row)].filter(Boolean).map(esc).join(' · ');
    const script = String(row.job_title || '').split('`')[0].trim();
    return `<article class="flow-row po-row is-${row.state}" data-po-row="${esc(k)}">${select}<span class="flow-stage is-${info.tone}">${info.label}</span><span class="flow-record-main"><a class="flow-title" href="${esc(photoURL(row))}" data-po-open-link="${esc(k)}" title="打开这条图文">${esc(row.timeline_name || script || row.timeline_uid)}</a>${old ? `<small class="po-old">超过 ${staleDays} 天</small>` : ''}<span class="flow-record-meta">${script && row.timeline_name ? `${esc(script)} · ` : ''}${meta}</span></span><span class="po-actions">${actions(row)}</span></article>`;
  }

  function paint(node,data,ctx) {
    const rows = data.items || [], counts = data.counts || {};
    if (ui.only && ui.only !== 'done' && !open.includes(ui.only)) ui.only = '';
    ui.selected = new Set([...ui.selected].filter(k => rows.some(row => keyOf(row) === k && selectable(row) && visible(row))));
    const shown = rows.filter(visible);
    const count = tab => rows.filter(row => inTab(row,tab)).length;
    const tabs = [['','要处理'],...open.filter(state => counts[state] || ui.only === state).map(state => [state,states[state].label]),['done','已结束']];
    const strip = `<div class="po-strip" role="group" aria-label="图文状态">${tabs.map(([key,label]) => `<button type="button" class="po-tab" data-po-only="${key}" aria-pressed="${ui.only === key}">${label}<small>${count(key)}</small></button>`).join('')}${ui.only === 'done' ? `<span class="po-strip-note">已上传 ${counts.uploaded || 0} · 已完结 ${counts.finished || 0} · 不做 ${counts.skip || 0}</span>` : ''}</div>`;
    const chosen = [...ui.selected].map(k => rows.find(row => keyOf(row) === k)).filter(Boolean);
    const pendingChosen = chosen.filter(row => row.state === 'pending'), exportedChosen = chosen.filter(row => row.state === 'exported');
    const old = shown.filter(row => row.state === 'exported' && stale(row) && !ui.selected.has(keyOf(row)));
    const foreignChosen = chosen.filter(row => row.translatable && !translating(row));
    const tongue = tongues[ui.market] || '外语';
    const foreignBar = foreignChosen.length ? `<span class="po-foreign"><select data-po-market aria-label="改成哪种语言">${Object.entries(tongues).map(([id,label]) => `<option value="${id}" ${id === ui.market ? 'selected' : ''}>${label}</option>`).join('')}</select><label title="不勾就只写好，等你在图文里看过再定稿"><input type="checkbox" data-po-auto-export ${ui.autoExport ? 'checked' : ''}>写完自动定稿导出</label><button type="button" class="primary compact" data-po-translate>出${tongue}版${foreignChosen.length === chosen.length ? '' : `（${foreignChosen.length} 条已定稿的）`}</button></span>` : '';
    const batch = chosen.length ? `<div class="po-batch"><span>已选 ${chosen.length} 条</span>${foreignBar}${pendingChosen.length ? `<button type="button" class="secondary compact" data-po-batch-skip>${pendingChosen.length === chosen.length ? '都不做' : `${pendingChosen.length} 条待决定的都不做`}</button>` : ''}${exportedChosen.length ? `<button type="button" class="secondary compact" data-po-batch-finish>${exportedChosen.length === chosen.length ? '都完结' : `${exportedChosen.length} 条待上传的都完结`}</button>` : ''}<button type="button" class="subtle compact" data-po-batch-cancel>取消</button></div>`
      : [shown.filter(row => row.state === 'pending').length > 1 ? '<button type="button" class="subtle compact" data-po-select-all>全选待决定</button>' : '',
         old.length ? `<button type="button" class="subtle compact" data-po-select-stale>选中超过 ${staleDays} 天没上传的（${old.length}）</button>` : ''].filter(Boolean).join('');
    const dayList = !chosen.length && ui.only !== 'done' && ui.only !== 'doing' && ui.only !== 'exported' ? days(rows) : [];
    if (!dayList.some(([key]) => key === ui.day)) ui.day = dayList[0]?.[0] || '';
    const dayRows = (dayList.find(([key]) => key === ui.day) || [])[1] || [];
    const dayBar = dayList.length ? `<span class="po-day"><label>按天导出无字版 <select data-po-day aria-label="选择哪天上传的视频">${dayList.map(([key,list]) => `<option value="${key}" ${key === ui.day ? 'selected' : ''}>${key.slice(5)} 周${weekday(key)} · ${list.length} 条</option>`).join('')}</select></label><button type="button" class="secondary compact" data-po-day-export>导出这天 ${dayRows.length} 条</button></span>` : '';
    const loose = [batch,dayBar].filter(Boolean).join('');
    const bar = chosen.length ? batch : loose ? `<div class="po-batch">${loose}</div>` : '';
    const body = shown.length ? shown.map(rowMarkup).join('')
      : `<div class="empty"><h3>${rows.length ? ui.only === 'done' ? '还没有已结束的图文' : '没有要处理的图文' : '还没有可做图文的视频'}</h3><p>${rows.length ? '「已结束」里是已上传、已完结和不做的。' : '视频上传到 Notion 或登记发布后，会出现在这里等你决定。'}</p></div>`;
    node.innerHTML = `${strip}${bar}${body}`;
    // While 无字版 exports run, look again sooner than the list's own 30-second poll.
    clearTimeout(ui.poll);
    if (rows.some(row => row.importing || translating(row))) ui.poll = setTimeout(() => {if (node.isConnected) ctx.reload();},8000);

    const find = k => rows.find(row => keyOf(row) === k);
    const call = async (row,decision) => {
      await api('/api/production/jobs/' + encodeURIComponent(row.job_id) + '/photo-decision',{method:'POST',body:{timeline:row.timeline_uid,decision}});
    };
    const launch = row => window.PhotoCarousel?.open({job:{id:row.job_id,product:row.product,title:row.job_title},api,uid:row.timeline_uid,timelineName:row.timeline_name,subtitle:row.job_title,onChange:() => ctx.reload()});
    const guarded = async work => {try {await work();} catch (error) {toast(error?.message || '操作暂未完成',true);ctx.reload();}};
    const many = (button,picked,decision,message) => {
      button.disabled = true;
      void guarded(async () => {
        for (const row of picked) {await call(row,decision);ui.recent.add(keyOf(row));}
        ui.selected.clear();toast(message(picked.length));window.TaskActivity?.refreshWork?.();ctx.reload();
      });
    };
    node.onchange = event => {
      const pick = event.target.closest('[data-po-day]');
      if (pick) {ui.day = pick.value;paint(node,data,ctx);return;}
      const market = event.target.closest('[data-po-market]');
      if (market) {ui.market = market.value;paint(node,data,ctx);return;}
      const auto = event.target.closest('[data-po-auto-export]');
      if (auto) {ui.autoExport = auto.checked;return;}
      const box = event.target.closest('[data-po-select]');if (!box) return;
      if (box.checked) ui.selected.add(box.dataset.poSelect);else ui.selected.delete(box.dataset.poSelect);
      paint(node,data,ctx);
    };
    node.onclick = event => {
      const title = event.target.closest('a.flow-title[data-po-open-link]');
      if (title && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) {
        event.preventDefault();const row = find(title.dataset.poOpenLink);if (row) launch(row);return;
      }
      const button = event.target.closest('button');if (!button || !node.contains(button)) return;
      const d = button.dataset;
      if ('poOnly' in d) {ui.only = d.poOnly;ui.selected.clear();paint(node,data,ctx);return;}
      if ('poSelectAll' in d) {shown.filter(row => row.state === 'pending').forEach(row => ui.selected.add(keyOf(row)));paint(node,data,ctx);return;}
      if ('poSelectStale' in d) {old.forEach(row => ui.selected.add(keyOf(row)));paint(node,data,ctx);return;}
      if ('poDayExport' in d) {
        const picked = dayRows;if (!picked.length) return;
        if (!confirm(`从达芬奇依次导出 ${ui.day.slice(5)} 上传的 ${picked.length} 条视频的无字版？\n一条导完再导下一条，导完自动抽帧，这些视频会变成「做中」。导出期间达芬奇会被占用。`)) return;
        button.disabled = true;
        void guarded(async () => {
          const reply = await api('/api/photo-sets/import-day',{method:'POST',body:{items:picked.map(row => ({job_id:row.job_id,timeline_uid:row.timeline_uid}))}});
          const started = reply?.started?.length || 0, skipped = reply?.skipped || [];
          const name = item => {const row = find(item.job_id + '|' + item.timeline_uid);return row?.timeline_name || row?.job_title || item.timeline_uid;};
          toast(`已排队导出 ${started} 条无字版` + (skipped.length ? `；${skipped.length} 条没导：${skipped.slice(0,3).map(item => `${name(item)}（${item.error}）`).join('、')}${skipped.length > 3 ? ' 等' : ''}` : '。'),skipped.length > 0 && !started);
          window.TaskActivity?.refreshWork?.();ctx.reload();
        });
        return;
      }
      if ('poTranslate' in d) {
        const picked = foreignChosen;if (!picked.length) return;
        if (!confirm(`把 ${picked.length} 条图文已定稿的版本改成${tongue}？\n画面和排版照搬，评论贴纸不带；一条写完再写下一条（每版约 1 分钟）${ui.autoExport ? '，写完自动定稿导出到「图文成品」（文件夹带地区后缀）' : '，写完先不导出，在图文里看过再定稿'}。原来的版本不动。`)) return;
        button.disabled = true;
        void guarded(async () => {
          const reply = await api('/api/photo-sets/translate',{method:'POST',body:{items:picked.map(row => ({job_id:row.job_id,timeline_uid:row.timeline_uid})),market:ui.market,export:ui.autoExport}});
          const started = reply?.started?.length || 0, skipped = reply?.skipped || [];
          const name = item => {const row = find(item.job_id + '|' + item.timeline_uid);return row?.timeline_name || row?.job_title || item.timeline_uid;};
          picked.forEach(row => ui.recent.add(keyOf(row)));ui.selected.clear();
          toast(`已排队出${tongue}版 ${started} 条` + (skipped.length ? `；${skipped.length} 条没排：${skipped.slice(0,3).map(item => `${name(item)}（${item.error}）`).join('、')}${skipped.length > 3 ? ' 等' : ''}` : '。'),skipped.length > 0 && !started);
          window.TaskActivity?.refreshWork?.();ctx.reload();
        });
        return;
      }
      if ('poBatchCancel' in d) {ui.selected.clear();paint(node,data,ctx);return;}
      if ('poBatchSkip' in d) {many(button,pendingChosen,'skip',n => `已标记 ${n} 条不做，可在「已结束」里撤销。`);return;}
      if ('poBatchFinish' in d) {
        if (exportedChosen.length > 1 && !confirm(`把 ${exportedChosen.length} 条还没上传的图文都完结？\n不会删除任何内容，之后可以在「已结束」里逐条撤销。`)) return;
        many(button,exportedChosen,'finish',n => `已完结 ${n} 条图文，可在「已结束」里撤销。`);return;
      }
      const row = find(d.poWant || d.poSkip || d.poOpen || d.poClear || d.poFinish || d.poReopen || d.poDelete);if (!row) return;
      if ('poDelete' in d) {button.closest('details').open = false;void window.PhotoCarousel?.askDelete({host:button.closest('[data-po-row]'),api,jobId:row.job_id,uid:row.timeline_uid,onDone:() => {ui.recent.delete(keyOf(row));ctx.reload();}});return;}
      if (!('poOpen' in d)) button.disabled = true;
      if ('poFinish' in d || 'poReopen' in d) void guarded(async () => {await call(row,'poFinish' in d ? 'finish' : 'reopen');ui.recent.add(keyOf(row));window.TaskActivity?.refreshWork?.();ctx.reload();});
      else if ('poSkip' in d) void guarded(async () => {await call(row,'skip');ui.recent.add(keyOf(row));ctx.reload();});
      else if ('poClear' in d) void guarded(async () => {await call(row,'clear');ui.recent.delete(keyOf(row));ctx.reload();});
      else if ('poWant' in d) void guarded(async () => {await call(row,'want');ctx.reload();launch(row);});
      else launch(row);
    };
  }

  // Another part of the list (the 积压 card) opens this tab on one state.
  function show(state) {ui.only = state === 'done' || open.includes(state) ? state : '';ui.selected.clear();}

  return {paint,show,states};
})();
