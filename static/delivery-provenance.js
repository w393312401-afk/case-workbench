'use strict';

// Read the version frozen with the delivered file, never today's selected version.
window.DeliveryProvenance = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const list = value => Array.isArray(value) ? value : [];
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || `#/${encodeURIComponent(product || 'all')}/${hash.replace(/^#/,'')}`;
  const production = (id,product,step,run = '') => url(`#production/${encodeURIComponent(id)}?${new URLSearchParams({step,...(run ? {run} : {})})}`,product);
  const link = (kind,label,href) => `<a data-provenance-kind="${esc(kind)}" href="${esc(href)}">${esc(label)}</a>`;
  const row = (label,content) => `<dt>${esc(label)}</dt><dd>${content}</dd>`;
  // A 64-hex fingerprint shows its first 12 characters and a saved time reads as a date; the exact value stays in the tooltip.
  const code = value => value ? `<small class="delivery-provenance-id" title="${esc(value)}">${esc(/^[0-9a-f]{64}$/i.test(String(value)) ? `${String(value).slice(0,12)}…` : value)}</small>` : '';
  // Saved times are UTC; read them in local time like the rest of the workbench.
  const when = value => {
    if (typeof readableDate === 'function') return readableDate(value);
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  };
  const stamp = value => value ? `<small class="delivery-provenance-id" title="${esc(value)}">${esc(when(value))}</small>` : '';

  function scriptRow(script,record,product) {
    const id = script.script_id || record.script_id, version = script.script_version ?? record.script_version;
    if (!id) return row('采用稿','未保留采用稿版本');
    const label = `${version ? `第 ${version} 版` : '采用稿'}${script.title ? ` · ${script.title}` : ''}`;
    return row('采用稿',link('script',label,url(`#content/${encodeURIComponent(id)}`,product))+code(id));
  }
  function subtitleRow(record) {
    // Timeline caption settings describe an editing project, not the actual exported file.
    const state = object(record.file?.subtitles);
    const confirmed = state.scope === 'actual_file' && state.confirmed === true && typeof state.present === 'boolean';
    const label = confirmed ? state.present ? '已登记：实际成片含字幕' : '已登记：实际成片无字幕' : '实际成片的字幕状态未登记';
    return row('字幕',`<span data-provenance-subtitles="${confirmed ? state.present ? 'present' : 'absent' : 'unknown'}">${esc(label)}</span>${confirmed ? stamp(state.checked_at) : '<small>以领取的视频为准；当前字幕设置不能证明这份成片的字幕。</small>'}`);
  }
  function video(record,owner,copyTake) {
    const source = object(record.source), script = object(source.script), take = object(source.take), timeline = object(source.timeline), edit = object(source.edit);
    const product = script.product || owner.product || 'all', id = record.job_id || owner.id;
    const rows = [scriptRow(script,record,product)];
    const copy = copyTake && take.id ? ` <button type="button" class="subtle compact" data-copy-take="${esc(take.id)}">复制编号</button>` : '';
    rows.push(row('配音',take.id && id ? link('voice',`配音版本 · ${take.id}`,production(id,product,'voice',take.id))+code(take.voice_name || take.voice_id)+copy : '未保留配音版本'));
    rows.push(row('时间线',timeline.unique_id && id ? `<span title="${esc(timeline.unique_id)}">${link('timeline',timeline.name || '登记时的时间线',production(id,product,'timeline',timeline.unique_id))}</span>` : '未保留时间线版本'));
    if (source.manual_completion?.at) rows.push(row('剪辑',`人工完成${stamp(source.manual_completion.at)}${edit.id && id ? link('edit',`此前剪辑记录 · ${edit.id}`,production(id,product,'edit',edit.id)) : ''}`));
    else if (edit.id && id) rows.push(row('剪辑',link('edit',`剪辑版本 · ${edit.id}`,production(id,product,'edit',edit.id))+stamp(edit.finished_at || edit.started_at)));
    else rows.push(row('剪辑','未保留剪辑或人工完成记录'));
    rows.push(subtitleRow(record));
    if (record.file?.sha256) rows.push(row('成片指纹',code(record.file.sha256)));
    const lines = list(script.lines).map(line => line.spoken || '').filter(Boolean).join('\n');
    return {id:record.id,rows:rows.join(''),extra:lines ? `<details class="delivery-provenance-text"><summary>查看这版口播正文</summary><pre>${esc(lines)}</pre></details>` : ''};
  }
  // rows: caller facts about the registration itself (when, from which file), kept in the same single fold.
  function render({kind,record,owner = {},rows = '',copyTake = false} = {}) {
    if (!record || kind !== 'video') return '';
    const data = video(record,owner,copyTake);
    return `<details class="delivery-provenance" data-provenance-record="${esc(data.id || '')}"><summary>查看这份成品的来源版本</summary><div class="delivery-provenance-body"><p class="context-note">以下按保存成品时的记录显示，后续选择不会改变这份来源。</p><dl>${data.rows}${rows}</dl>${data.extra}</div></details>`;
  }
  function mount(host,options) {if (host) host.innerHTML = render(options);}
  return {render,mount,when};
})();
