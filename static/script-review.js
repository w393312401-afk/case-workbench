'use strict';

// 口播稿审查（右侧抽屉）：候选稿页、采用稿页、制作单「脚本」区共用一套审查接口与持久化记录。
// 当前只对已配置专属审稿技能的产品（202H、198H -> sharpal-<型号>-script-review）开放入口。
window.ScriptReview = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const KIND = 'script_review';
  const SUPPORTED_PRODUCTS = new Set(['202H', '198H']);
  const VERDICT_SHORT = {pass:'可拍',revise:'需改',rewrite:'重写'};
  const VERDICT_FULL = {pass:'✅ 可直接拍/配',revise:'⚠️ 改完关键点再发',rewrite:'❌ 建议重写切口'};

  let dialog = null;
  let current = null;      // {target, options, state, selectedId, loading, error}
  let pollTimer = null;
  let tickTimer = null;
  const summaryCache = new Map();

  function productKey(product) {
    const raw = String(product || '').trim().toUpperCase();
    if (!raw) return '';
    const match = raw.match(/\b(\d{3}[A-Z]\d?)\b/);
    if (match) return match[1];
    return raw.replace(/^SHARPAL\s+/i, '').trim();
  }

  function supported(product) {
    return SUPPORTED_PRODUCTS.has(productKey(product));
  }

  function targetKey(target) {
    if (!target) return '';
    if (target.kind === 'candidate') return `candidate:${target.task_id}:${target.round_id}:${target.variant_index}`;
    if (target.kind === 'script') return `script:${target.script_id}`;
    if (target.kind === 'job') return `job:${target.job_id}`;
    return '';
  }

  function queryFor(target) {
    const params = new URLSearchParams({kind: target.kind});
    if (target.kind === 'candidate') {
      params.set('task_id', target.task_id);
      params.set('round_id', target.round_id);
      params.set('variant_index', String(target.variant_index));
    } else if (target.kind === 'script') {
      params.set('script_id', target.script_id);
    } else if (target.kind === 'job') {
      params.set('job_id', target.job_id);
    }
    return `/api/script-reviews?${params.toString()}`;
  }

  function elapsedText(iso) {
    const start = Date.parse(iso || '');
    if (!Number.isFinite(start)) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }

  function shortTime(iso) {
    const date = new Date(iso || '');
    if (Number.isNaN(date.getTime())) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function badgeMarkup(info) {
    if (!info) return '';
    if (info.status === 'running') return '<span class="script-review-dot is-running">审查中</span>';
    if (info.status === 'failed') return '<span class="script-review-dot is-failed">失败</span>';
    if (info.status === 'done' && info.adopted) return '<span class="script-review-dot is-pass">已采纳</span>';
    if (info.status === 'done' && info.verdict) {
      return `<span class="script-review-dot is-${esc(info.verdict)}">${esc(VERDICT_SHORT[info.verdict] || '已审')}</span>`;
    }
    if (info.status === 'done') return '<span class="script-review-dot is-pass">已审</span>';
    return '';
  }

  function paintButton(button, info) {
    if (!button || !button.isConnected) return;
    button.querySelector('.script-review-dot')?.remove();
    const badge = badgeMarkup(info);
    if (badge) button.insertAdjacentHTML('beforeend', badge);
  }

  function paintButtonsForKey(key, info) {
    if (!key) return;
    if (info) summaryCache.set(key, info);
    document.querySelectorAll(`[data-script-review-key="${CSS.escape(key)}"]`).forEach(btn => paintButton(btn, info));
  }

  async function syncButtons(root = document) {
    const buttons = [...(root || document).querySelectorAll('[data-script-review-key]')];
    if (!buttons.length) return;
    const keys = [...new Set(buttons.map(btn => btn.dataset.scriptReviewKey).filter(Boolean))];
    if (!keys.length) return;
    buttons.forEach(btn => {
      const cached = summaryCache.get(btn.dataset.scriptReviewKey);
      if (cached) paintButton(btn, cached);
    });
    try {
      const data = await api(`/api/script-reviews/summary?keys=${encodeURIComponent(keys.join(','))}`);
      const items = data?.items || {};
      keys.forEach(key => {
        if (items[key]) summaryCache.set(key, items[key]);
      });
      buttons.forEach(btn => {
        if (!btn.isConnected) return;
        const info = items[btn.dataset.scriptReviewKey];
        if (info) paintButton(btn, info);
      });
    } catch {
      // Badge sync is progressive enhancement; never block the page.
    }
  }

  function stopTimers() {
    if (pollTimer) {clearTimeout(pollTimer); pollTimer = null;}
    if (tickTimer) {clearInterval(tickTimer); tickTimer = null;}
  }

  function ensureDialog() {
    if (dialog && dialog.isConnected) return dialog;
    dialog = document.createElement('dialog');
    dialog.id = 'script-review-drawer';
    dialog.className = 'rev-drawer script-review-drawer';
    dialog.setAttribute('aria-labelledby', 'sr-drawer-title');
    dialog.addEventListener('close', () => {
      stopTimers();
      current = null;
    });
    dialog.addEventListener('click', event => {
      if (event.target === dialog) dialog.close();
    });
    document.body.append(dialog);
    return dialog;
  }

  function activeRecord() {
    if (!current?.state) return null;
    const history = current.state.history || [];
    if (current.selectedId) {
      const picked = history.find(item => item.id === current.selectedId);
      if (picked) return picked;
    }
    return current.state.latest || history[0] || null;
  }

  function buildRevisionInstruction(record) {
    const parts = ['按 202H 口播稿审查报告修改这条稿（保持成立的切口，只做最小必要修改）：'];
    if (record.revision_points) {
      parts.push('\n【关键修改要点】\n' + record.revision_points);
    }
    if (record.revised_spoken) {
      parts.push('\n【审查建议的改后口播参考】\n' + record.revised_spoken);
    }
    if (!record.revision_points && !record.revised_spoken && record.markdown) {
      parts.push('\n' + record.markdown.slice(0, 1800));
    }
    return parts.join('\n').trim();
  }

  const jobHref = id => {
    const hash = `#production/${encodeURIComponent(id)}`;
    return window.WorkbenchShell?.url?.(hash, current?.target?.product) || `#/${encodeURIComponent(current?.target?.product || 'all')}/${hash.slice(1)}`;
  };
  // Several models reviewing the same script at once share a batch id; they are compared and taken sentence by sentence.
  const MULTI_KEY = 'sr-multi-models';
  const batchOf = (state, id) => (state?.history || []).filter(item => id && item.batch === id)
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
  const anyRunning = state => (state?.history || []).some(item => item.status === 'running');
  const modelName = record => window.ModelPick?.describe(record.model, record.reasoning_effort) || record.model || '默认模型';
  function savedMulti() {
    try {const value = JSON.parse(localStorage.getItem(MULTI_KEY) || '[]'); return Array.isArray(value) ? value.filter(id => typeof id === 'string') : [];}
    catch {return [];}
  }
  function saveMulti(ids) {try {localStorage.setItem(MULTI_KEY, JSON.stringify(ids));} catch {}}
  function multiChosen() {
    const known = new Set((current?.models || []).map(model => model.id));
    return (current?.multi || []).filter(id => known.has(id));
  }
  function multiMarkup() {
    const models = current?.models || [];
    if (models.length < 2) return '';
    const chosen = new Set(multiChosen());
    const chips = models.map(model => `<label class="sr-multi-chip${chosen.has(model.id) ? ' is-on' : ''}"><input type="checkbox" data-sr-multi="${esc(model.id)}" ${chosen.has(model.id) ? 'checked' : ''}>${esc(model.name || model.id)}</label>`).join('');
    return `<div class="sr-multi" role="group" aria-label="多模型同时审查"><span class="sr-multi-label">多模型对比</span>${chips}<span class="sr-multi-hint">${chosen.size >= 2 ? `勾了 ${chosen.size} 个，会同时审查，结果逐句对照` : '勾 2–4 个模型同时审查，逐句对照挑改法'}</span></div>`;
  }

  // Ticks for one batch: per sentence, whose rewrite (or none) and whose picture (or none); extras are 新增/合并 items.
  function mixFor(batch, records) {
    current.mix ||= new Map();
    if (!current.mix.has(batch)) {
      const state = {spoken: {}, proof: {}, extra: {}};
      records.forEach(record => (record.adoption?.items || []).forEach(item => {
        if (item.sources?.length !== 1 || item.change === '新增') return;
        const n = item.sources[0];
        if (state.spoken[n] == null && item.spoken_changed && item.change === '必改') state.spoken[n] = `${record.id}:${item.index}`;
      }));
      current.mix.set(batch, state);
    }
    return current.mix.get(batch);
  }

  function mixPicks(mix) {
    const picks = [];
    const push = (key, field) => {
      if (!key) return;
      const at = key.lastIndexOf(':');
      picks.push({review_id: key.slice(0, at), index: Number(key.slice(at + 1)), [field]: true});
    };
    Object.values(mix.spoken).forEach(key => push(key, 'spoken'));
    Object.values(mix.proof).forEach(key => push(key, 'proof'));
    Object.entries(mix.extra).forEach(([key, on]) => {if (on) push(key, 'spoken');});
    return picks;
  }

  function compareMarkup(records) {
    const batch = records[0]?.batch || '';
    const kind = current.target.kind;
    const usable = records.filter(record => record.status === 'done' && record.adoption?.available);
    const heads = records.map(record => {
      const state = record.status === 'running' ? '<span class="sr-mix-state is-running">审查中…</span>'
        : record.status === 'done' ? `<span class="sr-verdict is-${esc(record.verdict || 'revise')}">${esc(VERDICT_SHORT[record.verdict] || '已审')}</span>${record.adoption && !record.adoption.available ? `<span class="sr-mix-state">${esc(record.adoption.reason || '不能逐句采纳')}</span>` : ''}`
        : `<span class="sr-mix-state is-failed">${esc(record.error || '没有完成')}</span>`;
      return `<div class="sr-mix-model"><b>${esc(modelName(record))}</b>${state}</div>`;
    }).join('');
    const reports = records.filter(record => record.status === 'done').map(record => {
      const rendered = typeof renderMarkdown === 'function' ? renderMarkdown(record.markdown || '') : `<pre>${esc(record.markdown || '')}</pre>`;
      return `<details class="sr-report-fold"><summary>${esc(modelName(record))} 的完整报告</summary><article class="sr-report markdown-body">${rendered}</article></details>`;
    }).join('');
    const adoptedOne = records.find(record => record.adopted);
    const adoptedNote = adoptedOne ? `<div class="sr-banner is-done sr-adopted" role="status"><span>${esc(shortTime(adoptedOne.adopted.at))} 已采纳${adoptedOne.adopted.note ? `：${esc(adoptedOne.adopted.note)}` : ''}。${adoptedOne.adopted.job_id ? ` <a href="${esc(jobHref(adoptedOne.adopted.job_id))}" data-sr-adopted-job>打开新制作单</a>` : ''}</span></div>` : '';
    if (!usable.length) {
      return `<section class="sr-mix" aria-label="多模型对照"><div class="sr-mix-models">${heads}</div>${adoptedNote}${records.some(record => record.status === 'running') ? '<p class="sr-mix-wait">各模型跑完后，这里会逐句列出它们的改法。</p>' : ''}</section>${reports}`;
    }
    const mix = mixFor(batch, usable);
    const original = {};
    usable.forEach(record => (record.adoption.items || []).forEach(item => {
      if (item.change === '新增') return;
      if (item.sources?.length === 1 && !original[item.sources[0]]) original[item.sources[0]] = {spoken: item.orig_spoken, zh: item.orig_zh, proof: item.orig_proof};
    }));
    const numbers = Object.keys(original).map(Number).sort((a, b) => a - b);
    const cellOf = {}, extras = [];
    usable.forEach(record => (record.adoption.items || []).forEach(item => {
      if (item.change === '未改') return;
      if (item.change === '新增' || (item.sources?.length || 0) > 1) {extras.push({record, item}); return;}
      (cellOf[item.sources[0]] ||= {})[record.id] = item;
    }));
    const changedNumbers = numbers.filter(n => cellOf[n]);
    const same = numbers.filter(n => !cellOf[n]);
    const rows = changedNumbers.map(n => {
      const orig = original[n] || {};
      const spokenKey = mix.spoken[n] || '';
      const origCell = `<td class="sr-mix-orig"><label class="sr-mix-opt${spokenKey ? '' : ' is-on'}"><input type="radio" name="sr-mix-sp-${n}" value="" data-sr-mix-spoken="${n}" ${spokenKey ? '' : 'checked'}><span><span class="sr-mix-keep">保持原句</span><span class="sr-mix-en">${esc(orig.spoken)}</span><span class="sr-mix-zh">${esc(orig.zh)}</span></span></label>${orig.proof ? `<span class="sr-mix-pic">画面：${esc(orig.proof)}</span>` : ''}</td>`;
      const cells = usable.map(record => {
        const item = cellOf[n][record.id];
        if (!item) return '<td class="sr-mix-none">没改</td>';
        const key = `${record.id}:${item.index}`;
        const tag = `<span class="sr-chg is-${CHANGE_CLASS[item.change] || 'sug'}">${esc(item.change)}</span>`;
        const why = item.reason ? `<span class="sr-mix-why">${esc(item.reason)}</span>` : '';
        const words = item.spoken_changed
          ? `<label class="sr-mix-opt${spokenKey === key ? ' is-on' : ''}"><input type="radio" name="sr-mix-sp-${n}" value="${esc(key)}" data-sr-mix-spoken="${n}" ${spokenKey === key ? 'checked' : ''}><span>${tag}${item.change === '删除' ? '<span class="sr-mix-en">（删掉这句）</span>' : `<span class="sr-mix-en">${esc(item.spoken)}</span><span class="sr-mix-zh">${esc(item.zh)}</span>`}${why}</span></label>`
          : `<div class="sr-mix-optless">${tag}<span class="sr-mix-zh">口播不改</span>${why}</div>`;
        const pic = item.proof_changed
          ? `<label class="sr-mix-pic-pick${mix.proof[n] === key ? ' is-on' : ''}"><input type="checkbox" data-sr-mix-proof="${n}" value="${esc(key)}" ${mix.proof[n] === key ? 'checked' : ''}>换画面：${esc(item.proof)}</label>` : '';
        return `<td>${words}${pic}</td>`;
      }).join('');
      return `<tr><th scope="row">${n}</th>${origCell}${cells}</tr>`;
    }).join('');
    const extraRows = extras.map(({record, item}) => {
      const key = `${record.id}:${item.index}`;
      const where = item.change === '新增' ? `第 ${item.after || 0} 句后新增` : `第 ${item.sources.join('、')} 句合并`;
      return `<label class="sr-mix-extra"><input type="checkbox" data-sr-mix-extra="${esc(key)}" ${mix.extra[key] ? 'checked' : ''}><span><b>${esc(modelName(record))}</b> · ${esc(where)}<span class="sr-mix-en">${esc(item.spoken)}</span><span class="sr-mix-zh">${esc(item.zh)}</span>${item.reason ? `<span class="sr-mix-why">${esc(item.reason)}</span>` : ''}</span></label>`;
    }).join('');
    const picks = mixPicks(mix);
    const spokenCount = picks.filter(pick => pick.spoken).length, proofCount = picks.filter(pick => pick.proof).length;
    const summary = spokenCount || proofCount ? `采纳 ${[spokenCount ? `${spokenCount} 句口播` : '', proofCount ? `${proofCount} 条画面` : ''].filter(Boolean).join('、')}` : '还没选要采纳的改法';
    const where = kind === 'candidate' ? '存为新一轮候选稿（不调 AI），原稿留在这一轮。' : kind === 'script' ? '存为采用稿的新版本，已有制作单继续用原来那版。'
      : spokenCount ? '采用稿存为新版本，并按新版新建一张制作单（要重新配音、剪辑）；这张单的配音和剪辑原样保留。' : '只写进这张制作单的意群表画面要求。';
    const label = kind === 'candidate' ? '采纳到新一轮' : kind === 'script' ? '采纳为新版本' : spokenCount ? '采纳并新建制作单' : '采纳画面到意群表';
    const head = `<tr><th scope="col">句</th><th scope="col">原句</th>${usable.map(record => `<th scope="col">${esc(modelName(record))}</th>`).join('')}</tr>`;
    return `<section class="sr-mix" aria-label="多模型逐句对照"><div class="sr-mix-models">${heads}</div>${adoptedNote}<div class="sr-mix-wrap"><table class="sr-mix-table">${head}${rows}</table></div>${same.length ? `<p class="sr-adopt-kept">第 ${esc(same.join('、'))} 句各模型都没改，原样保留。</p>` : ''}${extraRows ? `<div class="sr-mix-extras">${extraRows}</div>` : ''}<div class="sr-adopt-foot"><p><b>${esc(summary)}</b><span>${esc(where)}</span></p><div class="sr-adopt-actions"><button type="button" class="primary compact" data-sr-mix-adopt ${spokenCount || proofCount ? '' : 'disabled'}>${label}</button></div></div></section>${reports}`;
  }

  async function adoptMixed(records) {
    if (!current) return;
    const usable = records.filter(record => record.status === 'done' && record.adoption?.available);
    if (!usable.length) return;
    const mix = mixFor(records[0].batch, usable), target = current.target;
    const body = {kind: target.kind, review_id: usable[0].id, picks: mixPicks(mix)};
    if (target.kind === 'candidate') Object.assign(body, {task_id: target.task_id, round_id: target.round_id, variant_index: target.variant_index});
    else if (target.kind === 'script') body.script_id = target.script_id;
    else body.job_id = target.job_id;
    const button = dialog?.querySelector('[data-sr-mix-adopt]');
    if (button) button.disabled = true;
    try {
      const result = await api('/api/script-reviews/adopt', {method: 'POST', body});
      const options = current.options || {};
      dialog.close();
      if (typeof options.onAdopted === 'function') options.onAdopted(result, {pool: false});
      else if (typeof toast === 'function') toast(`已采纳：${result.note || ''}`);
    } catch (error) {
      if (button) button.disabled = false;
      if (typeof toast === 'function') toast(error?.message || '采纳失败', true);
    }
  }

  const CHANGE_CLASS = {'必改':'must','建议':'sug','画面':'pic','新增':'sug','删除':'must','合并':'sug','改动':'sug'};
  const changedItems = adoption => (adoption?.items || []).filter(item => item.change !== '未改');
  const takesSpoken = item => item.spoken_changed && !item.spoken_locked;

  // The ticks for one review: a must-fix and a suggestion start ticked, and so does each changed picture.
  function picksFor(record) {
    current.picks ||= new Map();
    if (!current.picks.has(record.id)) {
      const picks = new Map();
      changedItems(record.adoption).forEach(item => picks.set(item.index, {spoken: takesSpoken(item), proof: item.proof_changed, alt: false}));
      current.picks.set(record.id, picks);
    }
    return current.picks.get(record.id);
  }

  function adoptCounts(record) {
    const picks = picksFor(record);
    let spoken = 0, proof = 0;
    changedItems(record.adoption).forEach(item => {
      const pick = picks.get(item.index) || {};
      if (pick.spoken && takesSpoken(item)) spoken += 1;
      if (pick.proof && item.proof_changed) proof += 1;
    });
    return {spoken, proof};
  }

  function adoptMarkup(record) {
    const adoption = record.adoption;
    if (!adoption) return '';
    if (!adoption.available) {
      return adoption.reason ? `<div class="sr-banner is-warn sr-adopt-off" role="status"><span>${esc(adoption.reason)}</span></div>` : '';
    }
    const kind = current.target.kind, picks = picksFor(record), items = changedItems(adoption);
    const kept = (adoption.items || []).filter(item => item.change === '未改').map(item => item.no);
    const rows = items.map(item => {
      const pick = picks.get(item.index) || {};
      const tag = `<span class="sr-chg is-${CHANGE_CLASS[item.change] || 'sug'}">${esc(item.change)}</span>`;
      const where = item.change === '新增' ? `插在第 ${esc(item.after)} 句后` : `第 ${esc(item.no)} 句`;
      const head = `<div class="sr-adopt-head">${tag}<b>${where}</b>${item.refs?.length ? `<span class="sr-adopt-ref">${esc(item.refs.join(' '))}</span>` : ''}${item.reason ? `<span class="sr-adopt-why">${esc(item.reason)}</span>` : ''}</div>`;
      let words;
      if (item.spoken_changed) {
        const before = item.orig_spoken ? `<div class="sr-del">${esc(item.orig_spoken)}</div>` : '';
        const after = item.change === '删除' ? '<div class="sr-add">（删掉这句）</div>' : `<div class="sr-add">${esc(item.spoken)}</div>${item.zh ? `<div class="sr-zh">${esc(item.zh)}</div>` : ''}`;
        const alt = item.alternative && !item.spoken_locked ? `<div class="sr-alt" role="radiogroup" aria-label="第 ${esc(item.no)} 句用哪一版"><label><input type="radio" name="sr-alt-${item.index}" value="0" data-sr-alt="${item.index}" ${pick.alt ? '' : 'checked'}>默认句</label><label title="${esc(item.alternative.check || '')}"><input type="radio" name="sr-alt-${item.index}" value="1" data-sr-alt="${item.index}" ${pick.alt ? 'checked' : ''}>可换：${esc(item.alternative.spoken)}</label>${item.alternative.check ? `<span class="sr-alt-check">上线前核对：${esc(item.alternative.check)}</span>` : ''}</div>` : '';
        const locked = item.spoken_locked ? '<div class="sr-adopt-lock">制作单的口播跟着配音走，这句的口播改动要到采用稿上采纳。</div>' : '';
        words = before + after + alt + locked;
      } else {
        words = `<div class="sr-same">${esc(item.orig_spoken || item.spoken)} <span>（口播不改）</span></div>`;
      }
      const proof = item.proof_changed ? `<label class="sr-adopt-proof"><input type="checkbox" data-sr-pick-proof="${item.index}" ${pick.proof ? 'checked' : ''}>${item.spoken_changed ? '画面一起换' : '换画面'}：${item.orig_proof ? `<s>${esc(item.orig_proof)}</s> → ` : ''}<span>${esc(item.proof)}</span></label>` : '';
      const main = takesSpoken(item)
        ? `<input type="checkbox" class="sr-adopt-check" data-sr-pick-spoken="${item.index}" ${pick.spoken ? 'checked' : ''} aria-label="采纳第 ${esc(item.no)} 句的口播改动">`
        : '<span class="sr-adopt-check is-blank"></span>';
      const off = !(pick.spoken && takesSpoken(item)) && !(pick.proof && item.proof_changed);
      return `<div class="sr-adopt-item${off ? ' is-off' : ''}" data-sr-item="${item.index}">${main}<div class="sr-adopt-body">${head}${words}${proof}</div></div>`;
    }).join('');
    const counts = adoptCounts(record);
    const summary = counts.spoken || counts.proof
      ? `采纳 ${[counts.spoken ? `${counts.spoken} 句口播` : '', counts.proof ? `${counts.proof} 条画面` : ''].filter(Boolean).join('、')}`
      : '还没勾选要采纳的改动';
    const where = kind === 'candidate' ? '存为新一轮候选稿（不调 AI），原稿留在这一轮。' : kind === 'script' ? '存为采用稿的新版本，已有制作单继续用原来那版。'
      : counts.spoken ? '采用稿存为新版本，并按新版新建一张制作单（要重新配音、剪辑）；这张单的配音和剪辑原样保留。' : '只写进这张制作单的意群表画面要求。';
    const none = !(counts.spoken || counts.proof);
    const buttons = kind === 'candidate'
      ? `<button type="button" class="primary compact" data-sr-adopt ${none ? 'disabled' : ''}>采纳到新一轮</button><button type="button" class="secondary compact" data-sr-adopt="pool" ${none ? 'disabled' : ''}>采纳并入池</button>`
      : `<button type="button" class="primary compact" data-sr-adopt ${none ? 'disabled' : ''}>${kind === 'script' ? '采纳为新版本' : counts.spoken ? '采纳并新建制作单' : '采纳画面到意群表'}</button>`;
    return `<section class="sr-adopt" aria-label="逐句采纳"><div class="sr-adopt-bar"><b>逐句采纳</b><span>${items.length} 处改动${kept.length ? `，${kept.length} 句没动` : ''}</span><span class="sr-adopt-tools"><button type="button" class="subtle compact" data-sr-pick-all>全选</button><button type="button" class="subtle compact" data-sr-pick-must>只留必改</button></span></div>${rows}${kept.length ? `<p class="sr-adopt-kept">第 ${esc(kept.join('、'))} 句审查没改，原样保留。</p>` : ''}<div class="sr-adopt-foot"><p><b data-sr-adopt-summary>${esc(summary)}</b><span>${esc(where)}</span></p><div class="sr-adopt-actions">${buttons}</div></div></section>`;
  }

  async function adopt(record, pool) {
    if (!current || !record?.adoption?.available) return;
    const target = current.target, picks = picksFor(record);
    const body = {kind: target.kind, review_id: record.id, picks: [...picks].map(([index, pick]) => ({index, ...pick}))};
    if (target.kind === 'candidate') Object.assign(body, {task_id: target.task_id, round_id: target.round_id, variant_index: target.variant_index});
    else if (target.kind === 'script') body.script_id = target.script_id;
    else body.job_id = target.job_id;
    const buttons = [...(dialog?.querySelectorAll('[data-sr-adopt]') || [])];
    buttons.forEach(button => {button.disabled = true;});
    try {
      const result = await api('/api/script-reviews/adopt', {method: 'POST', body});
      const options = current.options || {};
      dialog.close();
      if (typeof options.onAdopted === 'function') options.onAdopted(result, {pool});
      else if (typeof toast === 'function') toast(`已采纳：${result.note || ''}`);
    } catch (error) {
      buttons.forEach(button => {button.disabled = false;});
      if (typeof toast === 'function') toast(error?.message || '采纳失败', true);
    }
  }

  function updateSummaryFromState(state) {
    const key = state?.target_key;
    const latest = state?.latest;
    if (!key || !latest) return;
    paintButtonsForKey(key, {
      id: latest.id,
      status: latest.status,
      verdict: latest.verdict || '',
      verdict_label: latest.verdict_label || '',
      script_hash: latest.script_hash || '',
      updated_at: latest.updated_at || '',
      count: (state.history || []).length,
      adopted: Boolean(latest.adopted),
    });
  }

  function schedulePoll() {
    stopTimers();
    if (!current || !dialog?.open) return;
    const latest = current.state?.latest;
    if (!latest || !anyRunning(current.state)) return;
    tickTimer = setInterval(() => {
      const node = dialog?.querySelector('[data-sr-elapsed]');
      if (node && current?.state?.latest?.created_at) {
        node.textContent = `已耗时 ${elapsedText(current.state.latest.created_at)}`;
      }
    }, 1000);
    pollTimer = setTimeout(async () => {
      if (!current || !dialog?.open) return;
      const target = current.target;
      const key = targetKey(target);
      try {
        const next = await api(queryFor(target));
        if (!current || targetKey(current.target) !== key) return;
        const wasRunning = anyRunning(current.state);
        current.state = next;
        // Follow the running review only while the reader has not picked another one from the history.
        if (!current.pinned) current.selectedId = next.latest?.batch ? `batch:${next.latest.batch}` : next.latest?.id || '';
        updateSummaryFromState(next);
        render();
        if (anyRunning(next)) {
          schedulePoll();
        } else if (wasRunning && next.latest?.status === 'done') {
          if (typeof toast === 'function') toast('口播稿审查已完成');
        }
      } catch (error) {
        if (!current || targetKey(current.target) !== key) return;
        current.error = error?.message || '读取审查进度失败';
        render();
      }
    }, 2500);
  }

  async function startReview() {
    if (!current) return;
    const target = current.target;
    const multi = multiChosen();
    const body = multi.length >= 2 ? {kind: target.kind, models: multi.map(model => ({model, reasoning_effort: null}))}
      : {kind: target.kind, ...(window.ModelPick?.value(KIND) || {})};
    if (target.kind === 'candidate') {
      body.task_id = target.task_id;
      body.round_id = target.round_id;
      body.variant_index = target.variant_index;
    } else if (target.kind === 'script') {
      body.script_id = target.script_id;
    } else if (target.kind === 'job') {
      body.job_id = target.job_id;
    }
    current.error = '';
    const startBtn = dialog?.querySelector('[data-sr-start]');
    if (startBtn) startBtn.disabled = true;
    try {
      const result = await api('/api/script-reviews/start', {method: 'POST', body});
      if (multi.length < 2) window.ModelPick?.used(KIND);
      if (!current || targetKey(current.target) !== targetKey(target)) return;
      current.state = {...(current.state || {}), ...result};
      current.selectedId = result.latest?.batch ? `batch:${result.latest.batch}` : result.latest?.id || '';
      current.pinned = false;
      updateSummaryFromState(current.state);
      render();
      schedulePoll();
    } catch (error) {
      if (!current) return;
      current.error = error?.message || '发起口播稿审查失败。';
      render();
    }
  }

  async function stopReview() {
    if (!current) return;
    const key = current.state?.target_key || targetKey(current.target);
    try {
      await api('/api/script-reviews/stop', {method: 'POST', body: {target_key: key}});
      const next = await api(queryFor(current.target));
      if (!current || targetKey(current.target) !== key) return;
      current.state = next;
      current.selectedId = next.latest?.id || '';
      updateSummaryFromState(next);
      stopTimers();
      render();
      if (typeof toast === 'function') toast('已请求停止审查');
    } catch (error) {
      if (typeof toast === 'function') toast(error?.message || '停止失败', true);
    }
  }

  function render() {
    if (!dialog || !current) return;
    const {target, options, state, loading, error} = current;
    const selectedBatch = String(current.selectedId || '').startsWith('batch:') ? current.selectedId.slice(6) : '';
    const batchRecords = selectedBatch ? batchOf(state, selectedBatch) : [];
    const record = selectedBatch ? null : activeRecord();
    const latest = state?.latest || null;
    const isRunning = anyRunning(state);
    const history = state?.history || [];
    const title = state?.title || target.title || '口播稿审查';
    const product = state?.product || target.product || '';
    const skillName = state?.skill || '产品专属审稿技能';
    const sourceText = target.kind === 'candidate' ? '候选稿' : target.kind === 'script' ? '采用稿' : '制作单脚本';

    let bodyHtml = '';
    if (loading) {
      bodyHtml = '<div class="sr-empty"><p>正在读取审稿记录…</p></div>';
    } else if (state && state.available === false) {
      bodyHtml = `<div class="sr-banner is-warn"><span>当前产品（${esc(product || '未指定')}）暂未配置专属审稿技能。</span></div>`;
    } else {
      const multiCount = multiChosen().length;
      bodyHtml += `<div class="sr-bar">${multiCount >= 2 ? '' : window.ModelPick ? window.ModelPick.markup(KIND) : ''}<div class="sr-bar-actions">${
        isRunning
          ? '<button type="button" class="danger-outline compact" data-sr-stop>停止审查</button>'
          : `<button type="button" class="primary compact" data-sr-start>${multiCount >= 2 ? `并发审查（${multiCount}）` : history.length ? '重新审查' : '开始审查'}</button>`
      }</div>${isRunning ? '' : multiMarkup()}</div>`;

      if (error) {
        bodyHtml += `<div class="sr-banner is-error" role="alert"><span>${esc(error)}</span></div>`;
      }

      if (isRunning && latest?.batch) {
        const running = batchOf(state, latest.batch);
        bodyHtml += `<div class="sr-banner is-running" role="status"><span>${running.map(item => `${esc(modelName(item))}：${item.status === 'running' ? '审查中' : item.status === 'done' ? '完成' : '没完成'}`).join(' · ')}</span><span class="sr-progress-meta" data-sr-elapsed>已耗时 ${esc(elapsedText(latest.created_at))}</span></div>`;
      } else if (isRunning) {
        const modelLabel = window.ModelPick?.describe(latest.model, latest.reasoning_effort) || latest.model || '默认模型';
        bodyHtml += `<div class="sr-banner is-running" role="status"><span>${esc(latest.progress || '正在执行四关审查与逐句画面匹配…')} · 模型：${esc(modelLabel)}</span><span class="sr-progress-meta" data-sr-elapsed>已耗时 ${esc(elapsedText(latest.created_at))}</span></div>`;
      } else if (state?.stale && record?.id === latest?.id) {
        bodyHtml += '<div class="sr-banner is-warn" role="status"><span>当前稿件内容已修改，下方是上一版口播的审查结果。</span><button type="button" class="secondary compact" data-sr-start>重新审查当前版</button></div>';
      }

      const entries = [], seenBatch = new Set();
      history.forEach(item => {
        if (!item.batch) {entries.push({item}); return;}
        if (seenBatch.has(item.batch)) return;
        seenBatch.add(item.batch);
        entries.push({batch: item.batch, items: batchOf(state, item.batch)});
      });
      if (entries.length > 1 || seenBatch.size) {
        bodyHtml += `<div class="sr-history"><span>历史记录：</span>${entries.map((entry, idx) => {
          const number = `第 ${entries.length - idx} 次`;
          if (entry.batch) {
            const first = entry.items[0] || {};
            const status = entry.items.some(item => item.status === 'running') ? ' · 审查中' : entry.items.some(item => item.adopted) ? ' · 已采纳' : '';
            return `<button type="button" class="sr-history-tab${selectedBatch === entry.batch ? ' is-active' : ''}" data-sr-pick="batch:${esc(entry.batch)}">${esc(`${number} · ${shortTime(first.created_at) || ''} · ${entry.items.length} 个模型对照${status}`)}</button>`;
          }
          const item = entry.item;
          const active = record && item.id === record.id;
          const label = `${number} · ${shortTime(item.created_at) || ''}${item.adopted ? ' · 已采纳' : item.verdict ? ` · ${VERDICT_SHORT[item.verdict] || ''}` : item.status === 'running' ? ' · 审查中' : item.status === 'failed' ? ' · 失败' : ''}`;
          return `<button type="button" class="sr-history-tab${active ? ' is-active' : ''}" data-sr-pick="${esc(item.id)}">${esc(label)}</button>`;
        }).join('')}</div>`;
      }

      if (selectedBatch) {
        bodyHtml += compareMarkup(batchRecords);
      } else if (!record) {
        bodyHtml += `<div class="sr-empty"><strong>还没有审查记录</strong><p>点击上方「开始审查」，将调用 <code>${esc(skillName)}</code> 按「筛人钩子 → 欲望聚焦 → 信任排雷 → 临门一脚」四关执行单稿深审，并输出改动后逐句口播与对应画面。</p></div>`;
      } else if (record.status === 'running') {
        bodyHtml += '<div class="sr-empty"><p>AI 正在逐句核对产品事实、转化数据与画面避坑，通常需要 20–60 秒。你可以先关掉这个抽屉继续看别的稿，结果会自动保存。</p></div>';
      } else if (record.status === 'failed' || record.status === 'cancelled') {
        bodyHtml += `<div class="sr-banner is-error"><span>${esc(record.error || '这次审查没有完成。')}</span></div>`;
      } else {
        const verdictClass = record.verdict ? `is-${esc(record.verdict)}` : 'is-revise';
        const verdictText = VERDICT_FULL[record.verdict] || record.verdict_label || '审查完成';
        const modelLabel = window.ModelPick?.describe(record.model, record.reasoning_effort) || record.model || '默认模型';
        const gatesHtml = (record.gates || []).length
          ? `<div class="sr-gates">${record.gates.map(g => `<span class="sr-gate${g.key ? ` is-${esc(g.key)}` : ''}" title="${esc(g.summary || '')}">${esc(g.name)}<b>${esc(g.status)}</b></span>`).join('')}</div>`
          : '';
        const actionButtons = [
          record.revised_spoken ? '<button type="button" class="secondary compact" data-sr-copy-spoken>复制改后口播</button>' : '',
          '<button type="button" class="subtle compact" data-sr-copy-md>复制完整报告</button>',
          typeof options?.onApplyRevision === 'function' ? `<button type="button" class="${record.adoption?.available ? 'subtle' : 'primary'} compact" data-sr-apply>把改稿要点填入继续修改框</button>` : '',
          typeof options?.onEditScript === 'function' ? '<button type="button" class="secondary compact" data-sr-edit>去逐句编辑</button>' : '',
        ].filter(Boolean).join('');

        bodyHtml += `<section class="sr-summary"><div class="sr-summary-top"><span class="sr-verdict ${verdictClass}">${esc(verdictText)}</span><span class="sr-meta">${esc(shortTime(record.finished_at || record.updated_at))} · ${esc(modelLabel)}</span></div>${gatesHtml}<div class="sr-result-actions">${actionButtons}</div></section>`;
        const rendered = typeof renderMarkdown === 'function' ? renderMarkdown(record.markdown || '') : `<pre>${esc(record.markdown || '')}</pre>`;
        const adoptable = record.adoption?.available;
        if (record.adopted) {
          const went = record.adopted.job_id ? ` <a href="${esc(jobHref(record.adopted.job_id))}" data-sr-adopted-job>打开新制作单</a>` : '';
          bodyHtml += `<div class="sr-banner is-done sr-adopted" role="status"><span>${esc(shortTime(record.adopted.at))} 已采纳${record.adopted.note ? `：${esc(record.adopted.note)}` : ''}。${went}</span></div>`;
        }
        if (!record.adopted?.job_id) bodyHtml += adoptMarkup(record);
        bodyHtml += adoptable
          ? `<details class="sr-report-fold"><summary>完整审查报告</summary><article class="sr-report markdown-body">${rendered}</article></details>`
          : `<article class="sr-report markdown-body">${rendered}</article>`;
      }
    }

    dialog.innerHTML = `<div class="rev-drawer-head sr-head"><div class="sr-title"><h2 id="sr-drawer-title">口播稿审查</h2><span class="sr-sub">${esc(sourceText)} · ${esc(title)}${product ? ` · ${esc(product)}` : ''} · 技能：${esc(skillName)}</span></div><button type="button" class="icon-button" data-sr-close aria-label="关闭口播审查">${typeof icon === 'function' ? icon('close') : '✕'}</button></div><div class="sr-body">${bodyHtml}</div>`;

    window.DrawerResize?.attach(dialog, 'script-review');
    void window.ModelPick?.mount(dialog);

    dialog.querySelector('[data-sr-close]')?.addEventListener('click', () => dialog.close());
    dialog.querySelectorAll('[data-sr-adopted-job]').forEach(link => link.addEventListener('click', () => dialog.close()));
    dialog.querySelector('.sr-multi')?.addEventListener('change', event => {
      const id = event.target?.dataset?.srMulti;
      if (!id || !current) return;
      const chosen = new Set(multiChosen());
      if (event.target.checked) {
        if (chosen.size >= 4) {event.target.checked = false; if (typeof toast === 'function') toast('一次最多同时用 4 个模型。', true); return;}
        chosen.add(id);
      } else chosen.delete(id);
      current.multi = [...chosen];
      saveMulti(current.multi);
      render();
    });
    const mixHost = dialog.querySelector('.sr-mix');
    if (mixHost && batchRecords.length) {
      const usable = batchRecords.filter(item => item.status === 'done' && item.adoption?.available);
      const mix = usable.length ? mixFor(selectedBatch, usable) : null;
      mixHost.addEventListener('change', event => {
        const input = event.target;
        if (!mix) return;
        if (input.dataset.srMixSpoken != null) mix.spoken[input.dataset.srMixSpoken] = input.value;
        else if (input.dataset.srMixProof != null) mix.proof[input.dataset.srMixProof] = input.checked ? input.value : '';
        else if (input.dataset.srMixExtra != null) mix.extra[input.dataset.srMixExtra] = input.checked;
        else return;
        const scroller = dialog.querySelector('.sr-body'), scroll = scroller?.scrollTop || 0;
        render();
        const next = dialog.querySelector('.sr-body');
        if (next) next.scrollTop = scroll;
      });
      mixHost.querySelector('[data-sr-mix-adopt]')?.addEventListener('click', () => void adoptMixed(batchRecords));
    }
    dialog.querySelectorAll('[data-sr-start]').forEach(btn => btn.addEventListener('click', () => void startReview()));
    dialog.querySelector('[data-sr-stop]')?.addEventListener('click', () => void stopReview());
    dialog.querySelectorAll('[data-sr-pick]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (!current) return;
        current.selectedId = btn.dataset.srPick;
        current.pinned = true;  // the reader chose this one; a running review finishing elsewhere does not take it away
        render();
      });
    });
    const adoptHost = dialog.querySelector('.sr-adopt');
    if (adoptHost && record) {
      const picks = picksFor(record);
      const refresh = () => {
        const scroller = dialog.querySelector('.sr-body'), scroll = scroller?.scrollTop || 0;
        render();
        const next = dialog.querySelector('.sr-body');
        if (next) next.scrollTop = scroll;
      };
      adoptHost.addEventListener('change', event => {
        const input = event.target;
        const index = Number(input.dataset.srPickSpoken ?? input.dataset.srPickProof ?? input.dataset.srAlt);
        if (!Number.isInteger(index)) return;
        const pick = picks.get(index) || {};
        if (input.dataset.srPickSpoken != null) pick.spoken = input.checked;
        else if (input.dataset.srPickProof != null) pick.proof = input.checked;
        else if (input.dataset.srAlt != null) {pick.alt = input.value === '1'; pick.spoken = true;}
        picks.set(index, pick);
        refresh();
      });
      adoptHost.querySelector('[data-sr-pick-all]')?.addEventListener('click', () => {
        changedItems(record.adoption).forEach(item => picks.set(item.index, {...(picks.get(item.index) || {}), spoken: takesSpoken(item), proof: item.proof_changed}));
        refresh();
      });
      adoptHost.querySelector('[data-sr-pick-must]')?.addEventListener('click', () => {
        changedItems(record.adoption).forEach(item => {
          const must = item.change === '必改';
          picks.set(item.index, {...(picks.get(item.index) || {}), spoken: must && takesSpoken(item), proof: must && item.proof_changed});
        });
        refresh();
      });
      adoptHost.querySelectorAll('[data-sr-adopt]').forEach(button => button.addEventListener('click', () => void adopt(record, button.dataset.srAdopt === 'pool')));
    }
    dialog.querySelector('[data-sr-copy-spoken]')?.addEventListener('click', () => {
      if (record?.revised_spoken) copyText(record.revised_spoken, '改动后口播已复制');
    });
    dialog.querySelector('[data-sr-copy-md]')?.addEventListener('click', () => {
      if (record?.markdown) copyText(record.markdown, '完整审查报告已复制');
    });
    dialog.querySelector('[data-sr-apply]')?.addEventListener('click', () => {
      if (!record || typeof options?.onApplyRevision !== 'function') return;
      const text = buildRevisionInstruction(record);
      dialog.close();
      options.onApplyRevision(text, record);
    });
    dialog.querySelector('[data-sr-edit]')?.addEventListener('click', () => {
      if (record?.revised_spoken && typeof copyText === 'function') {
        copyText(record.revised_spoken, '已复制改后口播，方便对照编辑');
      }
      dialog.close();
      options?.onEditScript?.(record);
    });
  }

  async function open(target, options = {}) {
    if (!target || !target.kind) return;
    const d = ensureDialog();
    stopTimers();
    current = {target, options, state: null, selectedId: '', loading: true, error: '', multi: savedMulti(), models: [], pinned: false};
    void window.ModelPick?.load?.().then(catalog => {
      if (!current || current.target !== target) return;
      current.models = (catalog?.models || []).filter(model => model && typeof model.id === 'string' && model.available !== false);
      render();
    });
    render();
    if (!d.open) d.showModal();
    const key = targetKey(target);
    try {
      const data = await api(queryFor(target));
      if (!current || targetKey(current.target) !== key) return;
      current.loading = false;
      current.state = data;
      current.selectedId = data.latest?.batch ? `batch:${data.latest.batch}` : data.latest?.id || '';
      updateSummaryFromState(data);
      render();
      if (anyRunning(data)) schedulePoll();
    } catch (error) {
      if (!current || targetKey(current.target) !== key) return;
      current.loading = false;
      current.error = error?.message || '无法读取审稿状态。';
      render();
    }
  }

  function targetFromKey(key) {
    const parts = String(key || '').split(':');
    if (parts[0] === 'candidate' && parts.length === 4 && /^\d+$/.test(parts[3])) return {kind:'candidate',task_id:parts[1],round_id:parts[2],variant_index:Number(parts[3])};
    if (parts[0] === 'script' && parts[1]) return {kind:'script',script_id:parts[1]};
    if (parts[0] === 'job' && parts[1]) return {kind:'job',job_id:parts[1]};
    return null;
  }

  // Opened from the task panel: wait for the reviewed script's page to draw its 口播审查 button and press it,
  // so adopting updates that page the same way as from the button. A page that never shows it opens the drawer alone.
  let waiting = null;
  function openWhenReady(key, {timeout = 8000} = {}) {
    const target = targetFromKey(key);
    if (!target) return;
    if (waiting) clearInterval(waiting.timer);
    const started = Date.now(), wanted = waiting = {key, timer: 0};
    const attempt = () => {
      if (waiting !== wanted) return;
      const button = [...document.querySelectorAll('.script-review-trigger[data-script-review-key]')].find(node => node.dataset.scriptReviewKey === key);
      if (button || Date.now() - started >= timeout) {
        clearInterval(wanted.timer);
        waiting = null;
        if (button) button.click();
        else void open(target);
      }
    };
    wanted.timer = setInterval(attempt, 150);
    setTimeout(attempt, 0);
  }

  return {supported, targetKey, open, openWhenReady, syncButtons};
})();
