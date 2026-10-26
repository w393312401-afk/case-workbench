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
  function carousel(record,owner,publication) {
    const post = record.delivery_id ? record : publication?.delivery_id === record.id && (!publication.delivery_sha256 || publication.delivery_sha256 === record.sha256) ? publication : null;
    let packageRecord = record;
    if (record.delivery_id) {
      // A cleaned ZIP can still have a read-only source snapshot on its publication.
      // Match both identity and bytes; never borrow the newest package's sources.
      const snapshot = object(record.delivery_snapshot);
      packageRecord = snapshot.id === record.delivery_id && snapshot.sha256 === record.delivery_sha256 ? snapshot
        : list(owner.deliveries).find(item => item.id === record.delivery_id && (!record.delivery_sha256 || item.sha256 === record.delivery_sha256)) || {};
    }
    const source = object(packageRecord.source), script = object(source.script);
    const product = script.product || owner.product || 'all', id = source.carousel_id || owner.id;
    const frozen = packageRecord.id ? packageRecord : record;
    const rows = [scriptRow(script,frozen,product)];
    const deliveryId = packageRecord.id || record.delivery_id;
    const available = list(owner.deliveries).some(item => item.id === deliveryId && (!record.delivery_sha256 || item.sha256 === record.delivery_sha256));
    if (deliveryId && id) rows.push(row('成品包',(record.delivery_id && !available ? `${esc(deliveryId)}<small>原发布来源，文件已清理。</small>`
      : link('delivery',deliveryId,production(id,product,'delivery',deliveryId)))+code(packageRecord.filename)));
    else rows.push(row('成品包','未保留成品包标识'));
    if (post?.delivery_id) rows.push(row('实际发布',`${esc(post.post_id || '已登记帖子')} · 使用 ${esc(post.delivery_id)}`));
    const slides = list(packageRecord.slides);
    rows.push(row('图片版本',slides.length ? `<ol class="delivery-provenance-slides">${slides.map(slide => {
      const snapshot = list(source.slides).find(item => item.number === slide.number) || {}, output = object(snapshot.output);
      const label = `第 ${slide.number} 张${output.version ? ` · 成图第 ${output.version} 版` : ''} · ${slide.output_id || '版本未保留'}`;
      return `<li>${slide.output_id && id ? link('image',label,production(id,product,'images',slide.output_id)) : esc(label)}${code(slide.sha256)}</li>`;
    }).join('')}</ol>` : '<span>这份发布记录的成品包快照不可读，图片版本未能核对。</span>'));
    if (packageRecord.sha256 || record.delivery_sha256) rows.push(row('成品包指纹',code(packageRecord.sha256 || record.delivery_sha256)));
    const caption = packageRecord.caption_en;
    const copy = list(source.slides).map(slide => [
      `第 ${slide.number} 张`,list(slide.title_lines).join(' / '),slide.body || ''
    ].filter(Boolean).join('\n')).join('\n\n');
    return {id:record.id,rows:rows.join(''),extra:[
      copy ? `<details class="delivery-provenance-text"><summary>查看保存时的逐张文案</summary><pre>${esc(copy)}</pre></details>` : '',
      caption ? `<details class="delivery-provenance-text"><summary>查看这包保存的发布文案</summary><pre>${esc(caption)}</pre></details>` : ''
    ].join('')};
  }
  // rows: caller facts about the registration itself (when, from which file), kept in the same single fold.
  function render({kind,record,owner = {},publication = null,rows = '',copyTake = false} = {}) {
    if (!record || (kind !== 'video' && kind !== 'carousel')) return '';
    const data = kind === 'video' ? video(record,owner,copyTake) : carousel(record,owner,publication);
    return `<details class="delivery-provenance" data-provenance-record="${esc(data.id || '')}"><summary>查看这份成品的来源版本</summary><div class="delivery-provenance-body"><p class="context-note">以下按保存成品时的记录显示，后续选择不会改变这份来源。</p><dl>${data.rows}${rows}</dl>${data.extra}</div></details>`;
  }
  function mount(host,options) {if (host) host.innerHTML = render(options);}
  return {render,mount,when};
})();
