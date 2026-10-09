'use strict';

function nativeSummary(items) {
  const count = items.reduce((total, item) => total + (item.current?.native_reference?.entries.length || 0), 0);
  const mode = state.nativeMode === 'entries' ? 'entries' : 'videos';
  return `<div class="archive-overview native-overview"><div><strong>${items.length} 条视频 · ${count} 条表达</strong><p>连同原句、场景、前后承接与复用边界一起保存。入选表达参考不代表流量或成交已验证。</p></div><div class="layout-switch native-mode-switch" role="group" aria-label="本土表达显示方式"><button type="button" data-native-mode="videos" class="${mode === 'videos' ? 'active' : ''}" aria-pressed="${mode === 'videos'}">按视频</button><button type="button" data-native-mode="entries" class="${mode === 'entries' ? 'active' : ''}" aria-pressed="${mode === 'entries'}">按表达</button></div></div>`;
}
function nativeEntryRows(items, query) {
  const needle = (query || '').trim().toLowerCase();
  const text = entry => [entry.original, entry.meaning_zh, entry.function, entry.context, entry.analysis, entry.adaptation, entry.boundary].join(' ').toLowerCase();
  const rows = [];
  for (const item of items) {
    const entries = item.current?.native_reference?.entries || [];
    const hits = needle ? entries.filter(entry => text(entry).includes(needle)) : entries;
    for (const entry of hits.length ? hits : entries) rows.push({ item, entry });
  }
  return rows;
}
function nativeEntriesView(rows) {
  return `<div class="native-list">${rows.map(({ item, entry }) => `<article class="native-row"><div class="native-row-head"><span class="badge">${entry.channel === 'speech' ? '口播' : '屏幕文字'}</span><strong>${esc(entry.function)}</strong><span class="native-row-time">${entry.start_sec}–${entry.end_sec} 秒</span></div><blockquote lang="en">${esc(entry.original)}</blockquote><p class="native-meaning">${esc(entry.meaning_zh)}</p><div class="native-row-foot"><span class="native-row-source">${esc(shortTitle(item))}</span><span class="native-row-actions"><button type="button" class="subtle" data-native-copy="${esc(entry.id)}">复制原句</button><button type="button" class="subtle" data-native-material="${esc(item.case_id+':'+entry.id)}">加入素材篮</button><a class="subtle" href="#case/${item.case_id}">在案例中定位</a></span></div></article>`).join('')}</div>`;
}
function bindNativeList(rows) {
  const byId = new Map(rows.map(({ entry }) => [entry.id, entry.original]));
  $$('[data-native-copy]').forEach(button => button.onclick = () => copyText(byId.get(button.dataset.nativeCopy) || '', '原句已复制'));
  bindNativeMaterials(rows);
}
function bindNativeMode() {
  $$('[data-native-mode]').forEach(button => button.onclick = () => {
    state.nativeMode = button.dataset.nativeMode;
    try { localStorage.setItem('native-library-mode', state.nativeMode); } catch {}
    $$('[data-native-mode]').forEach(other => {
      const active = other === button;
      other.classList.toggle('active', active);
      other.setAttribute('aria-pressed', String(active));
    });
    renderCards();
  });
}
function nativeSupplementalHighlights(item) {
  const entries = item.current?.native_reference?.entries || [];
  const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const entryText = entries.flatMap(entry => ['original', 'meaning_zh', 'function', 'context', 'analysis', 'adaptation', 'boundary'].map(key => normalize(entry[key])));
  // Only remove text that is already present verbatim. Similar ideas can carry additional detail.
  return (item.current?.highlights || []).filter(value => {
    const text = normalize(value);
    return text && !entryText.some(existing => existing.includes(text));
  });
}
function nativeDetail(item) {
  const ref = item.current?.native_reference;
  if (!ref) return '';
  const supplemental = nativeSupplementalHighlights(item);
  const paragraph = (label, value) => `<div class="native-context-row"><strong>${esc(label)}</strong><p>${esc(value)}</p></div>`;
  const note = (label, value, className = '', open = false) => `<details class="native-note ${className}"${open ? ' open' : ''}><summary>${esc(label)}</summary><div>${className === 'native-adaptation' ? '<span class="rewrite-badge">改写 · 非原话</span>' : ''}<p>${esc(value)}</p></div></details>`;
  const unchecked = !ref.market || /未核验|未核实|未确认|未知|待核|不明确/.test(ref.market);
  return `<section class="section-card native-reference"><div class="section-title-row"><h2>本土表达与场景</h2><button type="button" class="subtle" id="toggle-native-details" aria-expanded="false">全部展开</button></div><div class="native-status-badges"><span class="badge" title="内容语言">${esc(String(ref.language || '语言未提供').toUpperCase())}</span><span class="badge ${unchecked ? 'warning-badge' : ''}" title="${esc(ref.market || '发布市场与说话者地区未提供')}">${unchecked ? '⚠ 市场未核验' : esc(ref.market)}</span><span class="context-note">原文与改写分开保存</span></div><details class="native-context"><summary>画面、说话者与受众</summary>${paragraph('画面与使用场景', ref.scene)}${paragraph('说话者与表达姿态', ref.speaker)}${paragraph('受众与触发情境', ref.audience)}</details><div class="native-entries">${ref.entries.map(entry => {
    const timed = Number.isFinite(entry.start_sec) && Number.isFinite(entry.end_sec) && entry.start_sec >= 0 && entry.end_sec > entry.start_sec;
    const attributes = timed ? ` data-native-start="${entry.start_sec}" data-native-end="${entry.end_sec}"` : '';
    return `<article class="native-entry"${attributes}><div class="native-entry-head">${timed ? `<button type="button" class="timestamp-chip" data-native-seek="${entry.start_sec}" data-native-end="${entry.end_sec}" data-native-loop="true" title="跳到此处并循环这一段" aria-label="循环 ${entry.start_sec} 至 ${entry.end_sec} 秒">${entry.start_sec}–${entry.end_sec}s</button>` : '<span class="badge">时间待核实</span>'}<h3>${esc(entry.function)}</h3><span class="badge">${entry.channel === 'speech' ? '口播原句' : '屏显原文'}</span></div><blockquote class="native-original" lang="en">${esc(entry.original)}</blockquote><p class="native-meaning">${esc(entry.meaning_zh)}</p>${timed ? `<div class="native-play-actions"><button type="button" class="subtle" data-native-seek="${entry.start_sec}" data-native-end="${entry.end_sec}" data-native-loop="false">▶ 播放此段</button><button type="button" class="subtle" data-native-seek="${entry.start_sec}" data-native-end="${entry.end_sec}" data-native-loop="true" aria-pressed="false">↻ 循环此段</button></div>` : ''}<div class="native-row-actions"><button type="button" class="subtle" data-native-material="${esc(item.case_id+':'+entry.id)}">加入素材篮</button></div><div class="native-note-grid">${note('表达分析', entry.analysis, '', true)}${note('场景与前后承接', entry.context)}${note('复用示例', entry.adaptation, 'native-adaptation')}${note('⚠ 适用边界', entry.boundary, 'native-boundary')}</div></article>`;
  }).join('')}</div>${supplemental.length ? `<details class="native-supplemental"><summary>补充借鉴要点 <span>${supplemental.length}</span></summary><ul class="bullet-list">${supplemental.map(text => `<li>${esc(text)}</li>`).join('')}</ul></details>` : ''}</section>`;
}
function bindNativeMaterials(rows) {
  const byKey = new Map(rows.map(row => [row.item.case_id+':'+row.entry.id,row]));
  $$('[data-native-material]').forEach(button => button.onclick = async () => {
    const row = byKey.get(button.dataset.nativeMaterial);
    if (!row || !window.MaterialBasket) return;
    button.disabled = true;
    try { await window.MaterialBasket.add({type:'expression',source_id:row.item.case_id,product:row.item.product,text:row.entry.original,source_label:shortTitle(row.item),note:[row.entry.context,row.entry.boundary].filter(Boolean).join('\n')}); }
    finally { button.disabled = false; }
  });
}
function bindNativeDetail() {
  if (state.detail) bindNativeMaterials((state.detail.current?.native_reference?.entries || []).map(entry => ({item:state.detail,entry})));
  $$('[data-native-seek]').forEach(button => button.onclick = () => {
    const player = $('#case-player');
    if (!player) { toast('此案例的视频暂不可用。', true); return; }
    window.TranscriptSync?.playRange(Number(button.dataset.nativeSeek), Number(button.dataset.nativeEnd), {loop: button.dataset.nativeLoop === 'true', kind: 'native'});
  });
  const toggle = $('#toggle-native-details');
  if (toggle) toggle.onclick = () => {
    const notes = $$('.native-entry details.native-note');
    const expand = !notes.every(note => note.open);
    notes.forEach(note => { note.open = expand; });
    toggle.textContent = expand ? '全部折叠' : '全部展开';
    toggle.setAttribute('aria-expanded', String(expand));
  };
  $$('.native-entry details.native-note').forEach(note => note.addEventListener('toggle', () => {
    const expanded = $$('.native-entry details.native-note').every(other => other.open);
    if (toggle) { toggle.textContent = expanded ? '全部折叠' : '全部展开'; toggle.setAttribute('aria-expanded', String(expanded)); }
  }));
}
function nativeEntryEditor(entry = {}, index = 0) {
  return `<fieldset class="native-entry-editor" data-native-id="${esc(entry.id || crypto.randomUUID())}"><legend>表达 ${index + 1}</legend><div class="form-row">${field('native_channel','出处',entry.channel || 'speech',{select:{speech:'口播',on_screen:'屏幕文字'}})}${field('native_function','表达作用',entry.function || '')}</div><div class="form-row"><label class="field"><span>开始（秒）</span><input name="native_start" type="number" min="0" step="0.001" value="${entry.start_sec ?? ''}"></label><label class="field"><span>结束（秒）</span><input name="native_end" type="number" min="0" step="0.001" value="${entry.end_sec ?? ''}"></label></div>${[['original','原文'],['meaning_zh','中文原意'],['context','场景与前后承接'],['analysis','表达分析'],['adaptation','复用示例（非原话）'],['boundary','适用边界']].map(([key,label]) => field('native_'+key,label,entry[key] || '',{area:true})).join('')}<button type="button" class="subtle" data-native-remove>移除此条表达</button></fieldset>`;
}
function nativeEditor(current) {
  const ref = current.native_reference;
  return `<section class="editor-section native-editor"><h3>本土表达参考</h3><label class="native-toggle"><input type="checkbox" name="native_enabled" ${ref ? 'checked' : ''}>收录到本土表达参考</label><div id="native-editor-content" ${ref ? '' : 'hidden'}><p>保留真实原文和时间范围，结合画面分析；不要把未提供的表现填成已验证。</p><div class="form-row">${field('native_language','语言',ref?.language || 'en')}${field('native_market','市场及核实边界',ref?.market || '英语市场；地区未核实')}</div><label class="field"><span>原片时长（秒）</span><input name="native_duration" type="number" min="0.001" step="0.001" value="${ref?.duration_sec ?? ''}"></label>${field('native_scene','画面与使用场景',ref?.scene || '',{area:true})}${field('native_speaker','说话者与表达姿态',ref?.speaker || '',{area:true})}${field('native_audience','受众与触发情境',ref?.audience || '',{area:true})}<div id="native-editor-entries">${(ref?.entries || []).map(nativeEntryEditor).join('')}</div><button type="button" class="secondary" id="native-add-entry">添加表达</button></div></section>`;
}
function bindNativeEditor() {
  const enabled = $('[name="native_enabled"]');
  enabled.onchange = () => { $('#native-editor-content').hidden = !enabled.checked; };
  const bind = () => $$('[data-native-remove]').forEach(button => button.onclick = () => { button.closest('[data-native-id]').remove(); state.editor.dirty = true; });
  $('#native-add-entry').onclick = () => { $('#native-editor-entries').insertAdjacentHTML('beforeend',nativeEntryEditor({},$$('[data-native-id]').length)); state.editor.dirty = true; bind(); };
  bind();
}
function collectNativeReference(current, value) {
  if (!$('[name="native_enabled"]').checked) { delete current.native_reference; return; }
  const ref = {language:value('native_language'),market:value('native_market'),duration_sec:Number(value('native_duration')),scene:value('native_scene'),speaker:value('native_speaker'),audience:value('native_audience'),entries:[]};
  for (const box of $$('[data-native-id]')) {
    const get = name => $(`[name="native_${name}"]`,box).value.trim();
    const entry = {id:box.dataset.nativeId,start_sec:Number(get('start')),end_sec:Number(get('end'))};
    for (const key of ['channel','function','original','meaning_zh','context','analysis','adaptation','boundary']) entry[key] = get(key);
    if (get('start') === '' || get('end') === '') throw new Error('请填写表达的开始和结束时间。');
    ref.entries.push(entry);
  }
  if (!ref.entries.length) throw new Error('请至少添加一条表达，或取消收录到本土表达参考。');
  if (![ref.language,ref.market,ref.scene,ref.speaker,ref.audience].every(Boolean) || !Number.isFinite(ref.duration_sec) || ref.duration_sec <= 0) throw new Error('请补齐本土表达的场景、受众、语言与原片时长。');
  for (const entry of ref.entries) {
    if (!['function','original','meaning_zh','context','analysis','adaptation','boundary'].every(key => entry[key])) throw new Error('请补齐每条表达的原文、分析、复用示例和适用边界。');
    if (!(entry.start_sec >= 0 && entry.end_sec > entry.start_sec && entry.end_sec <= ref.duration_sec)) throw new Error('表达时间范围须位于原片时长内。');
  }
  current.native_reference = ref;
}
