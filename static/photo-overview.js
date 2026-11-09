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
  // 默认只列要处理的（导出了还没上传 Notion 也算）；已上传、已完结和不做折叠，点上方的状态数可单独查看。
  const open = new Set(['pending','want','doing','exported']);
  const ui = {only:'',showDone:false,selected:new Set(),recent:new Set()};
  const keyOf = row => row.job_id + '|' + row.timeline_uid;
  const day = value => String(value || '').slice(5,10);
  const finishedDay = value => {const date = new Date(value);return Number.isNaN(date.getTime()) ? '' : `${String(date.getMonth() + 1).padStart(2,'0')}/${String(date.getDate()).padStart(2,'0')}`;};
  const visible = row => ui.only ? row.state === ui.only : open.has(row.state) || ui.recent.has(keyOf(row)) || ui.showDone;

  function actions(row) {
    const k = esc(keyOf(row));
    const go = (attr,label,cls = 'secondary') => `<button type="button" class="${cls} compact" ${attr}="${k}">${label}</button>`;
    if (row.state === 'pending') return go('data-po-want','做','primary') + go('data-po-skip','不做');
    if (row.state === 'want') return go('data-po-open','开始做','primary') + go('data-po-finish','完结') + go('data-po-clear','撤销');
    const remove = go('data-po-delete','删除','danger-outline');
    if (row.state === 'doing') return go('data-po-open','继续做','primary') + go('data-po-finish','完结') + remove;
    if (row.state === 'exported') return go('data-po-open','上传 Notion','primary') + go('data-po-finish','完结') + remove;
    if (row.state === 'finished') return go('data-po-open','查看') + go('data-po-reopen','撤销') + remove;
    if (row.state === 'uploaded') return go('data-po-open','查看') + remove;
    return go('data-po-want','改成要做') + go('data-po-clear','撤销');
  }

  function rowMarkup(row) {
    const info = states[row.state], k = keyOf(row);
    const select = row.state === 'pending' ? `<input type="checkbox" class="flow-select" data-po-select="${esc(k)}" aria-label="选择 ${esc(row.timeline_name || row.job_title)}" ${ui.selected.has(k) ? 'checked' : ''}>` : '<span aria-hidden="true"></span>';
    const meta = [row.uploaded ? '视频已上传 Notion' : '视频已登记发布', row.since ? day(row.since) : '', row.notion_works?.length ? `图文 ${row.notion_works.join('、')} 版已上传` : '', row.reason ? `原因：${row.reason}` : '',row.state === 'finished' && row.finished_at ? `${finishedDay(row.finished_at)} 手动完结` : ''].filter(Boolean).map(esc).join(' · ');
    return `<article class="flow-row po-row is-${row.state}" data-po-row="${esc(k)}">${select}<span class="flow-stage is-${info.tone}">${info.label}</span><span class="flow-record-main"><span class="flow-title">${esc(row.timeline_name || row.job_title || row.timeline_uid)}</span><span class="flow-record-meta">${meta}</span></span><span class="po-actions">${actions(row)}</span></article>`;
  }

  function paint(node,data,ctx) {
    const rows = data.items || [], counts = data.counts || {};
    ui.selected = new Set([...ui.selected].filter(k => rows.some(row => keyOf(row) === k && row.state === 'pending')));
    const shown = rows.filter(visible), hidden = rows.length - shown.length;
    const chips = Object.entries(states).map(([key,info]) => `<button type="button" class="po-chip is-${info.tone}" data-po-only="${key}" aria-pressed="${ui.only === key}"><b>${counts[key] || 0}</b><span>${info.label}</span></button>`).join('');
    const batch = ui.selected.size ? `<div class="po-batch"><span>已选 ${ui.selected.size} 条</span><button type="button" class="secondary compact" data-po-batch-skip>都不做</button><button type="button" class="subtle compact" data-po-batch-cancel>取消</button></div>`
      : counts.pending > 1 ? '<div class="po-batch"><button type="button" class="subtle compact" data-po-select-all>全选待决定</button></div>' : '';
    const groups = [];
    for (const row of shown) {
      let group = groups.find(entry => entry.id === row.job_id);
      if (!group) groups.push(group = {id:row.job_id,title:row.job_title,rows:[]});
      group.rows.push(row);
    }
    const body = groups.length ? groups.map(group => `<div class="flow-group po-group"><span class="po-group-title">${esc(group.title || group.id)}</span><small>${group.rows.length}</small></div>${group.rows.map(rowMarkup).join('')}`).join('')
      : `<div class="empty"><h3>${rows.length ? '没有要处理的图文' : '还没有可做图文的视频'}</h3><p>${rows.length ? '点上面的状态数可以查看已上传、已完结和不做的。' : '视频上传到 Notion 或登记发布后，会出现在这里等你决定。'}</p></div>`;
    const fold = !ui.only && hidden > 0 ? `<button type="button" class="flow-finished-fold" data-po-fold>已上传 / 已完结 / 不做 ${hidden} 条<small>点开查看</small></button>` : ui.showDone && !ui.only ? '<button type="button" class="flow-finished-fold" data-po-fold>收起已上传 / 已完结 / 不做</button>' : '';
    node.innerHTML = `<div class="po-strip" role="group" aria-label="图文状态">${chips}</div>${batch}${body}${fold}`;

    const find = k => rows.find(row => keyOf(row) === k);
    const call = async (row,decision) => {
      await api('/api/production/jobs/' + encodeURIComponent(row.job_id) + '/photo-decision',{method:'POST',body:{timeline:row.timeline_uid,decision}});
    };
    const launch = row => window.PhotoCarousel?.open({job:{id:row.job_id},api,uid:row.timeline_uid,timelineName:row.timeline_name,onChange:() => ctx.reload()});
    const guarded = async work => {try {await work();} catch (error) {toast(error?.message || '操作暂未完成',true);ctx.reload();}};
    node.onchange = event => {
      const box = event.target.closest('[data-po-select]');if (!box) return;
      if (box.checked) ui.selected.add(box.dataset.poSelect);else ui.selected.delete(box.dataset.poSelect);
      paint(node,data,ctx);
    };
    node.onclick = event => {
      const button = event.target.closest('button');if (!button || !node.contains(button)) return;
      const d = button.dataset;
      if ('poOnly' in d) {ui.only = ui.only === d.poOnly ? '' : d.poOnly;paint(node,data,ctx);return;}
      if ('poFold' in d) {ui.showDone = !ui.showDone;paint(node,data,ctx);return;}
      if ('poSelectAll' in d) {rows.filter(row => row.state === 'pending').forEach(row => ui.selected.add(keyOf(row)));paint(node,data,ctx);return;}
      if ('poBatchCancel' in d) {ui.selected.clear();paint(node,data,ctx);return;}
      if ('poBatchSkip' in d) {
        const picked = [...ui.selected].map(find).filter(Boolean);button.disabled = true;
        void guarded(async () => {
          for (const row of picked) {await call(row,'skip');ui.recent.add(keyOf(row));}
          ui.selected.clear();toast(`已标记 ${picked.length} 条不做，可在「不做」里撤销。`);ctx.reload();
        });
        return;
      }
      const row = find(d.poWant || d.poSkip || d.poOpen || d.poClear || d.poFinish || d.poReopen || d.poDelete);if (!row) return;
      if ('poDelete' in d) {void window.PhotoCarousel?.askDelete({host:button.closest('[data-po-row]'),api,jobId:row.job_id,uid:row.timeline_uid,onDone:() => {ui.recent.delete(keyOf(row));ctx.reload();}});return;}
      if (!('poOpen' in d)) button.disabled = true;
      if ('poFinish' in d || 'poReopen' in d) void guarded(async () => {await call(row,'poFinish' in d ? 'finish' : 'reopen');ui.recent.add(keyOf(row));window.TaskActivity?.refreshWork?.();ctx.reload();});
      else if ('poSkip' in d) void guarded(async () => {await call(row,'skip');ui.recent.add(keyOf(row));ctx.reload();});
      else if ('poClear' in d) void guarded(async () => {await call(row,'clear');ui.recent.delete(keyOf(row));ctx.reload();});
      else if ('poWant' in d) void guarded(async () => {await call(row,'want');ctx.reload();launch(row);});
      else launch(row);
    };
  }

  return {paint,states};
})();
