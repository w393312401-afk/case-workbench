'use strict';

// Pre-publish adversarial review of the exported video. Unresolved 必须修 block 登记发布 until each is
// marked 已修改 or 接受风险; the review itself never reads performance data.
window.PrepublishReview = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const base = id => `/api/production/jobs/${encodeURIComponent(id)}/prepublish`;
  const severity = {must_fix:['必须修','is-must'],suggest:['建议','is-suggest']};
  const confidence = {high:'高',medium:'中',low:'低'};
  const timers = new WeakMap();
  const eligible = job => ['approved','awaiting_review'].includes(job.status) && !job.archived;
  const when = value => {const date = new Date(value || '');return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});};

  function finding(review,item) {
    const [label,tone] = severity[item.severity] || severity.suggest, done = item.resolution;
    const actions = item.severity === 'suggest' ? '' : done
      ? `<span class="prepublish-resolved">${esc(done.label)}${done.note ? ` · ${esc(done.note)}` : ''}</span><button type="button" class="subtle compact" data-resolve="" data-finding="${esc(item.id)}">撤销</button>`
      : `<button type="button" class="secondary compact" data-resolve="fixed" data-finding="${esc(item.id)}">已修改</button><button type="button" class="subtle compact" data-resolve="accepted" data-finding="${esc(item.id)}">接受风险</button>`;
    return `<li class="prepublish-finding ${tone}${done ? ' is-resolved' : ''}"><div class="prepublish-finding-head"><span class="prepublish-chip ${tone}">${label}</span><small>${esc(item.layer)} · ${esc(item.at)} · 把握${esc(confidence[item.confidence] || '中')}</small><span class="prepublish-finding-actions">${actions}</span></div><p>${esc(item.problem)}${item.consequence ? `<span class="prepublish-consequence"> → ${esc(item.consequence)}</span>` : ''}</p>${item.fix ? `<p class="prepublish-fix">改法：${esc(item.fix)}</p>` : ''}</li>`;
  }

  function sheets(job,review) {
    const names = review.report?.sheets || [];
    if (!names.length) return '';
    return `<details class="prepublish-sheets"><summary>审查用的拼图</summary>${names.map(name => `<a href="${base(job.id)}/${encodeURIComponent(review.id)}/sheets/${name}" target="_blank" rel="noopener">${name === 'opening.jpg' ? '开头 0–3 秒' : name.startsWith('detail') ? `开头原画 ${Number(name.slice(6,8))}` : name.replace('shots','镜头 ').replace('.jpg','')}</a>`).join('')}</details>`;
  }

  function completed(job,review,blocking) {
    const report = review.report || {}, open = review.findings.filter(item => item.severity !== 'suggest' && !item.resolution).length;
    return `<article class="prepublish-result" data-review="${esc(review.id)}">
      <p class="prepublish-meta">${esc(review.file?.name)} · ${esc(when(review.finished_at))} · ${report.duration ?? '–'} 秒 · ${report.shots ?? '–'} 个镜头</p>
      ${blocking ? `<p class="video-publication-warning">还有 ${blocking} 条「必须修」没有确认，登记发布会被拦下。</p>` : open ? '' : '<p class="prepublish-clear">必须修和待确认都已处理。</p>'}
      ${review.summary ? `<p class="prepublish-summary">${esc(review.summary)}</p>` : ''}
      ${review.findings.length ? `<h3 class="prepublish-kill">杀手清单</h3><ol class="prepublish-findings">${review.findings.map(item => finding(review,item)).join('')}</ol>` : '<p class="context-note">没有发现问题。</p>'}
      ${review.premortem ? `<p class="prepublish-premortem"><strong>事前验尸</strong>${esc(review.premortem)}<small>发布后复盘起草时会拿真实表现核对这条预测。</small></p>` : ''}
      ${review.ab_tests?.length ? `<div class="prepublish-ab"><strong>A/B 测试</strong><ul>${review.ab_tests.map(item => `<li>${esc(item)}</li>`).join('')}</ul></div>` : ''}
      ${sheets(job,review)}</article>`;
  }

  function render(host,job,data,state) {
    const reviews = data.reviews || [], running = reviews.find(row => row.status === 'running');
    const latest = reviews.find(row => row.id === data.latest_id), lastFailed = reviews[0] && reviews[0].status !== 'completed' && reviews[0].status !== 'running' ? reviews[0] : null;
    const path = state.path ?? (reviews[0]?.file?.path || '');
    const form = eligible(job) ? `<form class="prepublish-form"><label class="field"><span>导出成片的完整路径</span><input name="file_path" required maxlength="2000" autocomplete="off" value="${esc(path)}" placeholder="D:\\导出\\Lab261022-202-3.mp4"></label><button class="primary" type="submit" ${running ? 'disabled' : ''}>${latest ? '重新审查' : '开始审查'}</button></form>`
      : '<p class="context-note">剪辑审查完成、导出成片后可以发起。</p>';
    host.innerHTML = `<div class="section-title-row"><h2>发布前审查 <small>开头3秒 · 为什么要买 · 会不会被封 · 事前验尸</small></h2></div>
      <p class="context-note">用没参与制作的视角攻击导出的成片；不看表现数据。「必须修」逐条确认前不能登记发布。</p>
      ${running ? `<p class="production-busy">正在审查 ${esc(running.file?.name)} · ${esc(running.phase || '处理中')} <button type="button" class="subtle compact" data-cancel="${esc(running.id)}">取消</button></p>` : ''}
      ${lastFailed ? `<p class="ai-error">上次审查${lastFailed.status === 'cancelled' ? '已取消' : '没有完成'}：${esc(lastFailed.error || '原因未记录')}</p>` : ''}
      ${latest ? completed(job,latest,data.blocking) : ''}
      ${form}
      ${reviews.filter(row => row.status === 'completed').length > 1 ? `<details class="prepublish-history"><summary>历次审查（${reviews.filter(row => row.status === 'completed').length}）</summary><ul>${reviews.filter(row => row.status === 'completed').map(row => `<li>${esc(when(row.finished_at))} · ${esc(row.file?.name)} · ${row.findings.filter(item => item.severity === 'must_fix').length} 条必须修${row.id === data.latest_id ? ' · 当前' : ''}</li>`).join('')}</ul></details>` : ''}`;
  }

  async function load(host,options,state) {
    const {job,api} = options;
    clearTimeout(timers.get(host));
    if (!host.isConnected) return;
    let data;
    try {data = await api(base(job.id));}
    catch (problem) {host.innerHTML = `<h2>发布前审查</h2><p class="ai-error">${esc(problem.message || '暂时读不到审查记录。')}</p>`;return;}
    if (!host.isConnected) return;
    render(host,job,data,state);
    bind(host,options,state,data);
    options.onLoaded?.(data);
    if (data.running) timers.set(host,setTimeout(() => load(host,options,state),3000));
  }

  function bind(host,options,state,data) {
    const {job,api} = options, reload = () => load(host,options,state);
    const form = host.querySelector('.prepublish-form');
    form?.addEventListener('input',() => {state.path = form.elements.file_path.value;});
    form?.addEventListener('submit',async event => {
      event.preventDefault();
      const button = form.querySelector('button');button.disabled = true;
      try {await api(base(job.id),{method:'POST',body:{request_id:crypto.randomUUID(),file_path:form.elements.file_path.value.trim()}});await reload();}
      catch (problem) {button.disabled = false;toast(problem.message || '没能发起审查。',true);}
    });
    host.querySelector('[data-cancel]')?.addEventListener('click',async event => {
      event.currentTarget.disabled = true;
      try {await api(`${base(job.id)}/${encodeURIComponent(event.currentTarget.dataset.cancel)}/cancel`,{method:'POST',body:{}});}
      catch (problem) {toast(problem.message || '没能取消。',true);}
      await reload();
    });
    host.querySelectorAll('[data-resolve]').forEach(button => button.addEventListener('click',async () => {
      const resolution = button.dataset.resolve || null;
      const note = resolution === 'accepted' ? (prompt('接受这条风险的原因（可不填）') ?? null) : '';
      if (note === null) return;
      button.disabled = true;
      try {
        await api(`${base(job.id)}/${encodeURIComponent(data.latest_id)}/findings`,{method:'POST',body:{finding_id:button.dataset.finding,resolution,note}});
        await reload();options.onChanged?.();
      } catch (problem) {button.disabled = false;toast(problem.message || '没能保存。',true);}
    }));
  }

  function mount(host,options) {
    if (!host) return;
    const state = {};
    host.innerHTML = '<h2>发布前审查</h2><p class="context-note">读取中…</p>';
    load(host,options,state);
  }
  return {mount};
})();
