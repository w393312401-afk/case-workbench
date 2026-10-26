'use strict';

// A read-only checklist of the current work. Links locate inputs; they never rerun a job.
window.ProductionNeeds = (() => {
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const fpsOf = value => ({'23.976':24000/1001,'29.97':30000/1001,'59.94':60000/1001}[String(value)] || Number(value));
  function collect(record,kind) {
    if (!record?.id || record.archived) return [];
    const items = [], seen = new Set();
    const add = (key,label,detail,step,extra = {}) => {
      if (seen.has(key)) return;seen.add(key);
      const query = new URLSearchParams({step});
      for (const field of ['run','line','slide','gap','question','start','end']) if (extra[field] !== undefined && extra[field] !== null) query.set(field,String(extra[field]));
      items.push({key,label,detail,step,...extra,href:url(`#production/${encodeURIComponent(record.id)}?${query}`,record.product)});
    };
    if (kind === 'carousel') {
      const slides = record.slides || [];
      let confirmed = 0;
      for (const slide of slides) {
        const output = (slide.outputs || []).find(item => item.id === slide.output_id), valid = output?.approved === true && !output.stale;
        if (valid) {confirmed++;continue;}
        const usable = (slide.outputs || []).some(item => !item.stale);
        const title = [slide.title_zh,slide.role].map(value => String(value || '').trim()).find(value => /[\u3400-\u9fff]/.test(value)) || '图片';
        if (!slide.base && !usable) add(`slide-base-${slide.number}`,`第 ${slide.number} 张 · 待选底图`,title,'pick',{slide:slide.number});
        else add(`slide-output-${slide.number}`,`第 ${slide.number} 张 · ${output?.stale ? '已选成图失效' : usable ? '成图待确认选用' : '待出图'}`,title,'images',{slide:slide.number});
      }
      if (slides.length && confirmed === slides.length) {
        const current = (record.deliveries || []).find(item => item.current);
        if (!current) add('package','当前图片尚未整理成品包','先核对所用图片，再整理可领取的版本。','delivery');
        else if (!record.publication?.post_id || record.publication_current === false) add('publication','当前成品包尚未登记发布','已发布时登记实际使用的包和帖子；未发布可继续领取。','delivery',{run:current.id});
      }
      for (const run of record.generations || []) if (['failed','interrupted','cancelled'].includes(run.status)) {
        const remaining = (run.slides || []).filter(number => slides.some(slide => slide.number === number && !(slide.outputs || []).some(output => output.approved && !output.stale && output.id === slide.output_id)));
        if (remaining.length && run === (record.generations || []).at(-1)) add(`image-run-${run.id}`,'最近出图任务未完成',run.error || `第 ${remaining.join('、')} 张仍待处理。`,'images',{run:run.id});
      }
      return items;
    }
    const takes = record.takes || [], selected = takes.find(item => item.id === record.selected_take);
    if (!selected) {
      add('voice',takes.length ? '还没有选用配音' : '还没有生成配音',record.voicing ? '配音正在生成，可查看运行进度。' : takes.length ? '试听并选用要制作的那一版。' : '进入配音步骤核对文字和音色。','voice');
      return items;
    }
    const badGroups = (selected.groups || []).filter(group => group.coverage !== undefined && group.coverage < .6);
    if (selected.quality?.status === 'failed' || badGroups.length) add('voice-alignment','配音意群对齐待核对',badGroups.length ? `第 ${badGroups.map(group => group.number).join('、')} 句对齐不足，请试听。` : '对齐检查未通过，请核对这版配音。','voice',{run:selected.id});
    const timelines = (record.timelines || []).filter(item => !item.archived && item.take_id === selected.id);
    const building = record.timeline_building;
    const waitingEdit = [...(record.edits || [])].reverse().find(item => item.waiting_timeline && item.take_id === selected.id);
    if (!timelines.length && waitingEdit) {
      if (waitingEdit.status !== 'running') add('pending-edit','自动接续剪辑未开始',waitingEdit.error || '请核对原建线任务和剪辑记录。','edit',{run:waitingEdit.id});
      return items;
    }
    if (!timelines.length && building && (!building.take_id || building.take_id === selected.id)) {
      if (building.group_markers === true) add('pending-edit','可以先提交剪辑','本次时间线建好后自动接着剪，无需回来再次提交。','edit');
      return items;
    }
    if (!timelines.length) {add('timeline','这版配音还没有可用时间线','进入达芬奇时间线步骤新建，或展开历史时间线恢复。','timeline');return items;}
    let latestCount = 0;
    for (const timeline of timelines) {
      const edit = [...(record.edits || [])].reverse().find(item => item.timeline_uid === timeline.unique_id);
      if (!edit) {if (timeline.task_status === 'completed') latestCount++;continue;}latestCount++;
      const manual = record.manual_completion;
      const report = edit.report || {}, summary = edit.summary, gaps = summary?.gaps || report.gaps || [];
      const skill = edit.completion_policy === 'skill_delivery', validation = edit.validation;
      const reviewable = skill ? validation ? validation.scope === 'current_coverage' && validation.status === 'awaiting_review' && validation.saved === true && Array.isArray(summary?.picture_gaps) && !summary.picture_gaps.length : report.saved === true && !gaps.length : validation?.status === 'awaiting_review';
      const approved = edit.status === 'approved' || ['awaiting_review','completed'].includes(edit.status) && edit.approval && reviewable && !edit.result_pending && (edit.status !== 'completed' || skill);
      if (approved || (record.status === 'approved' && manual?.take_id === selected.id && manual.timeline_uid === timeline.unique_id)) continue;
      if (edit.status === 'running') continue;
      const fps = fpsOf(summary?.fps || timeline.fps);
      gaps.forEach((gap,index) => {
        if (gap.resolved === true || gap.status === 'resolved') return;
        const start = Number.isInteger(gap.start) && fps > 0 ? gap.start/fps : Number.isFinite(gap.seconds) ? gap.seconds : null;
        const end = Number.isInteger(gap.end) && fps > 0 ? gap.end/fps : start;
        const groups = start === null ? [] : (selected.groups || []).filter(group => typeof group.start === 'number' && typeof group.end === 'number' && group.start < (end === start ? start+.01 : end) && group.end > start);
        const lineText = groups.length ? `第 ${groups.map(group => group.number).join('、')} 句` : start === null ? '对应区间待核对' : `第 ${start.toFixed(2)} 秒`;
        add(`gap-${edit.id}-${index}`,`${lineText} · ${gap.status === 'unverified' ? '素材待核实' : '缺素材'}`,[gap.need || gap.name || '请核对该区间需要的画面',timelines.length > 1 ? timeline.name : ''].filter(Boolean).join(' · '),'edit',{run:edit.id,gap:index,...(Number.isInteger(gap.start) ? {start:gap.start} : {}),...(Number.isInteger(gap.end) ? {end:gap.end} : {})});
      });
      (report.questions || []).forEach((question,index) => add(`question-${edit.id}-${index}`,'剪辑问题待确认',String(question),'edit',{run:edit.id,question:index}));
      if (['failed','interrupted','cancelled','needs_repair','verification_failed','unverified'].includes(edit.status)) add(`edit-${edit.id}`,'这次剪辑仍需处理',edit.error || edit.readback_error || '进入该次剪辑结果核对问题与恢复入口。','edit',{run:edit.id});
      else if (!gaps.length && !report.questions?.length && ['completed','awaiting_review'].includes(edit.status)) add(`review-${edit.id}`,'当前剪辑待人工审查','核对画面、字幕、声音和实际导出。','edit',{run:edit.id});
    }
    if (!latestCount && record.status !== 'approved') add('edit','当前时间线还没有剪辑结果','进入 AI 剪辑与人工审查步骤。','edit');
    // Registering the post is the order's normal next step, shown once by the 下一步 bar, not a gap.
    return items;
  }
  function mount(host,{record,kind,compact = false,onVisit = null}) {
    if (!host) return;
    const items = collect(record,kind);host.hidden = !items.length;host.classList.add('production-needs');
    host.innerHTML = items.length ? `<div class="section-title-row"><h2>待处理与缺口 <small>${items.length} 项</small></h2></div><ul class="production-needs-list">${items.map(item => `<li><a data-need-key="${esc(item.key)}" href="${esc(item.href)}"><strong>${esc(item.label)}</strong><span>${esc(item.detail || '')}</span><small>定位处理入口 →</small></a></li>`).join('')}</ul>${compact ? '' : '<p class="context-note">点击定位到具体内容；选用、生成、登记和达芬奇定位仍使用对应页面的操作。</p>'}` : '';
    if (onVisit) host.querySelectorAll('a[data-need-key]').forEach(link => link.addEventListener('click',event => onVisit(event,link)));
  }
  async function mountRun(host,run,{onVisit = null} = {}) {
    const id = run?.owner_id, kind = /^CAR-[0-9a-f]{12}$/.test(id || '') ? 'carousel' : /^PRD-[0-9a-f]{12}$/.test(id || '') ? 'video' : null;
    if (!host || !kind) {if (host) host.hidden = true;return;}
    host.dataset.needsOwner = id;
    const sequence = (Number(host.dataset.needsSequence) || 0) + 1;host.dataset.needsSequence = String(sequence);
    const current = () => host.isConnected && host.dataset.needsOwner === id && Number(host.dataset.needsSequence) === sequence;
    host.hidden = false;host.innerHTML = '<p class="context-note" role="status">正在读取具体待处理事项…</p>';
    try {
      const data = await api(`/api/production/${kind === 'carousel' ? 'carousels' : 'jobs'}/${encodeURIComponent(id)}`);
      if (!current()) return;
      const record = kind === 'carousel' ? data.carousel : data.job;
      if (record?.id !== id || (run.product && !window.WorkbenchShell?.matchesProduct(record.product,run.product))) throw new Error('任务来源不一致');
      mount(host,{record,kind,compact:true,onVisit});
    } catch (error) {if (current()) {host.hidden = false;host.innerHTML = `<p class="context-note">待处理事项暂时无法读取：${esc(error.message || '请重试')}</p><button type="button" class="subtle" data-needs-retry>重新读取</button>`;host.querySelector('button').onclick = () => void mountRun(host,run,{onVisit});}}
  }
  return {collect,mount,mountRun};
})();
