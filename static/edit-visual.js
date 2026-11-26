'use strict';

// 剪辑说明的时间轴对照：意群、原片、成片和逐镜头差异上下对齐，点一个镜头看大图对比，可在达芬奇定位。
// 数据来自 /api/production/jobs/<制作单>/edits/<剪辑>/visual，缩略图按需从原片和素材原件抽帧。
window.EditVisual = (() => {
  const STATUS = {exact:'一致',close:'接近',substitute:'换了动作',missing:'留空'};
  const cache = new Map();
  const seconds = (frame,fps) => `${(frame / fps).toFixed(1)}s`;
  const overlaps = (item,row) => item.start < row.end && row.start < item.end;
  const base = (jobId,editId) => `/api/production/jobs/${encodeURIComponent(jobId)}/edits/${encodeURIComponent(editId)}`;

  // A small markdown reader for the editor's write-up: paragraphs, bullets (two levels), bold, code and tables.
  function inline(text) {
    return esc(text).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  }
  function markdown(text) {
    const lines = String(text || '').split(/\r?\n/), out = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {index++;continue;}
      const heading = line.match(/^\s*#{1,6}\s+(.+?)\s*#*\s*$/);
      if (heading) {out.push(`<h4 class="ev-heading">${inline(heading[1])}</h4>`);index++;continue;}
      if (/^\s*\|/.test(line) && /^\s*\|?[\s:|-]*-{3,}/.test(lines[index + 1] || '')) {
        const cells = row => row.trim().replace(/^\||\|$/g,'').split('|').map(cell => cell.trim());
        const head = cells(line);index += 2;
        const body = [];
        while (index < lines.length && /^\s*\|/.test(lines[index])) body.push(cells(lines[index++]));
        out.push(`<div class="ev-table-wrap"><table class="production-table compact"><thead><tr>${head.map(cell => `<th>${inline(cell)}</th>`).join('')}</tr></thead><tbody>${body.map(row => `<tr>${row.map(cell => `<td>${inline(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        const items = [];
        while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
          const nested = /^\s{2,}/.test(lines[index]), text = inline(lines[index].replace(/^\s*[-*]\s+/,''));
          if (nested && items.length) items[items.length - 1].children.push(text);
          else items.push({text,children:[]});
          index++;
        }
        out.push(`<ul>${items.map(item => `<li>${item.text}${item.children.length ? `<ul>${item.children.map(child => `<li>${child}</li>`).join('')}</ul>` : ''}</li>`).join('')}</ul>`);
        continue;
      }
      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !/^\s*[-*]\s+/.test(lines[index]) && !/^\s*\|/.test(lines[index]) && !/^\s*#{1,6}\s+/.test(lines[index])) paragraph.push(lines[index++].trim());
      out.push(`<p>${inline(paragraph.join(' '))}</p>`);
    }
    return out.join('');
  }

  function markup(job,edit,{client,open}) {
    return `<details class="edit-visual" data-edit-visual="${esc(edit.id)}" data-edit-job="${esc(job.id)}" ${open ? 'open' : ''}><summary>${esc(client)}的剪辑说明</summary><div class="ev-body" data-ev-body><p class="context-note">正在读取剪辑说明…</p></div></details>`;
  }

  const countOf = rows => rows.reduce((counts,row) => {
    if (row.status) counts[row.status] = (counts[row.status] || 0) + Math.max(1,(row.shots || []).length);
    return counts;
  },{});

  // 节奏报告：一行结论，展开只列要看的位置（切在词中间、画面晚到、超出时长范围的镜头）。
  function rhythm(data) {
    const value = data.rhythm;
    if (!value || !value.headline) return '';
    const fps = data.fps, at = frame => seconds(frame,fps), cuts = value.cuts;
    const rows = [];
    if (value.sections.length) rows.push(`各段平均镜头时长：${value.sections.map(part => `${part.name} ${part.average} 秒（${part.shots} 个）`).join('，')}；最短 ${value.shortest} 秒，最长 ${value.longest} 秒。`);
    if (cuts) rows.push(`${cuts.total} 个切点：${cuts.on} 个踩在词的起点，${cuts.pause} 个在停顿里，${cuts.mid} 个切在词中间。`);
    else rows.push('这版配音没有留下逐字时间，没法核对切点和口播的位置。');
    (cuts ? cuts.items : []).forEach(cut => rows.push(cut.late
      ? `${at(cut.frame)}：第 ${cut.line} 句开口后 ${Math.round(cut.offset)} 帧才换画面（“${cut.word}”）。`
      : `${at(cut.frame)}：切在“${cut.word}”中间，比这个词开口晚 ${Math.round(cut.offset)} 帧。`));
    value.outside.forEach(shot => rows.push(`${at(shot.start)}–${at(shot.end)}：这个镜头 ${shot.seconds} 秒，${shot.problem === 'short' ? '短于 1.2 秒' : '长于 4 秒'}。`));
    if (value.repeats.length) rows.push(`相邻两个镜头是同一类画面的有 ${value.repeats.length} 处：${value.repeats.map(item => `${at(item.start)} ${item.kind}`).join('；')}。`);
    return `<details class="ev-rhythm"><summary>节奏：${esc(value.headline)}</summary><ul>${rows.map(row => `<li>${esc(row)}</li>`).join('')}</ul></details>`;
  }

  function render(host,data,ctx) {
    if (!data.available || !data.rows.length) {
      host.innerHTML = `${data.reason ? `<p class="context-note">${esc(data.reason)}</p>` : ''}${data.available ? rhythm(data) : ''}<div class="ev-text">${markdown(data.text)}</div>`;
      return;
    }
    const {fps,total,rows} = data, counts = countOf(rows);
    const paragraphs = String(data.text || '').split(/\n\s*\n/), lead = paragraphs.shift() || '', rest = paragraphs.join('\n\n');
    const width = (start,end) => `${Math.max(0,(Math.min(end,total) - start) / total * 100).toFixed(4)}`;
    const thumb = (kind,number,label) => `<img loading="lazy" alt="${esc(label)}" src="${base(ctx.job.id,ctx.edit.id)}/frame/${kind}/${number}" data-ev-img>`;
    const lineLane = data.lines.length ? `<div class="ev-lane-name">配音意群</div><div class="ev-lane ev-lines">${data.lines.map(line => `<span class="ev-line" data-ev-width="${width(line.start,line.end)}" data-ev-left="${width(0,line.start)}" title="#${line.number} ${esc(line.zh || line.spoken)}">#${line.number} ${esc(line.zh || line.spoken)}</span>`).join('')}</div>` : '';
    const origLane = data.original ? `<div class="ev-lane-name">原片</div><div class="ev-lane ev-thumbs">${data.original.map(shot => `<span class="ev-cell" data-ev-width="${width(shot.start,shot.end)}" data-ev-left="${width(0,shot.start)}" title="原片镜头 ${shot.number}">${thumb('orig',shot.number,`原片镜头 ${shot.number}`)}</span>`).join('')}</div>` : '';
    const cutLane = `<div class="ev-lane-name">成片</div><div class="ev-lane ev-thumbs">${data.cut.map(item => `<span class="ev-cell" data-ev-width="${width(item.start,item.end)}" data-ev-left="${width(0,item.start)}" title="${esc([item.code,item.label].filter(Boolean).join(' · ') || '成片片段')}">${thumb('cut',item.index,item.code || '成片片段')}</span>`).join('')}</div>`;
    const rowLane = `<div class="ev-lane-name">${data.replica ? '差异' : '镜头'}</div><div class="ev-lane ev-rows" role="group" aria-label="逐镜头，点击查看">${rows.map((row,index) => `<button type="button" class="ev-row st-${esc(row.status || (index % 2 ? 'none-b' : 'none'))}" data-ev-row="${index}" data-ev-width="${width(row.start,row.end)}" data-ev-left="${width(0,row.start)}" title="${esc(`${/^\d/.test(row.label) ? `镜头 ${row.label}` : row.label}${row.status ? ` · ${STATUS[row.status]}` : ''}${row.note ? ` · ${row.note}` : ''}`)}">${esc(row.label)}</button>`).join('')}${data.gaps.map(gap => `<span class="ev-gap" data-ev-width="${width(gap.start,gap.end)}" data-ev-left="${width(0,gap.start)}" title="${esc(gap.need || '缺素材')}"></span>`).join('')}</div>`;
    const ticks = [];
    for (let second = 0; second * fps < total; second += total / fps > 40 ? 10 : 5) ticks.push(`<span data-ev-left="${width(0,second * fps)}">${second}s</span>`);
    const shown = data.inferred && data.counts ? data.counts : counts;
    const chips = data.replica ? `<div class="ev-chips">${Object.keys(STATUS).filter(key => Number(shown[key]) > 0).map(key => `<span class="ev-chip"><i class="st-${key}"></i>${STATUS[key]} ${shown[key]}</span>`).join('')}${data.inferred ? `<small title="这次剪辑的说明没有逐镜头标状态，颜色按「差在哪」的写法判断，以 AI 自己的统计为准">色块按说明文字推断，可能和统计差一两个</small>` : ''}</div>` : '';
    host.innerHTML = `${lead ? `<div class="ev-text ev-lead">${markdown(lead)}</div>` : ''}${chips}${rhythm(data)}<div class="ev-strip">${lineLane}${origLane}${cutLane}${rowLane}<div class="ev-ruler">${ticks.join('')}</div></div><div class="ev-detail" data-ev-detail aria-live="polite"></div>${rest.trim() ? `<details class="ev-rest"><summary>完整说明</summary><div class="ev-text">${markdown(rest)}</div></details>` : ''}`;
    host.querySelectorAll('[data-ev-width]').forEach(node => {node.style.width = `${node.dataset.evWidth}%`;node.style.left = `${node.dataset.evLeft}%`;});
    host.querySelectorAll('.ev-ruler [data-ev-left]').forEach(node => {node.style.left = `${node.dataset.evLeft}%`;});
    host.querySelectorAll('[data-ev-img]').forEach(image => image.addEventListener('error',() => {image.hidden = true;image.parentElement?.classList.add('ev-no-frame');},{once:true}));
    const first = rows.findIndex(row => row.status === 'substitute' || row.status === 'missing');
    const select = index => showRow(host,data,ctx,Math.max(0,Math.min(rows.length - 1,index)));
    host.querySelectorAll('[data-ev-row]').forEach(button => {
      button.onclick = () => select(Number(button.dataset.evRow));
      button.onkeydown = event => {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        select(Number(button.dataset.evRow) + (event.key === 'ArrowRight' ? 1 : -1));
        host.querySelector(`[data-ev-row="${host.dataset.evSelected}"]`)?.focus();
      };
    });
    select(first >= 0 ? first : 0);
  }

  function showRow(host,data,ctx,index) {
    const row = data.rows[index], fps = data.fps;
    host.dataset.evSelected = index;
    host.querySelectorAll('[data-ev-row]').forEach(button => button.classList.toggle('selected',Number(button.dataset.evRow) === index));
    const origins = (data.original || []).filter(shot => (row.shots || []).includes(shot.number));
    const cuts = data.cut.filter(item => overlaps(item,row));
    const spoken = data.lines.filter(line => overlaps(line,row));
    const big = (kind,number,label) => `<figure class="ev-big"><img alt="${esc(label)}" src="${base(ctx.job.id,ctx.edit.id)}/frame/${kind}/${number}" data-ev-img><figcaption>${esc(label)}</figcaption></figure>`;
    const field = (label,value) => value ? `<div class="ev-field"><b>${label}</b><span>${esc(value)}</span></div>` : '';
    const title = /^\d/.test(row.label) ? `镜头 ${row.label}` : row.label;
    const detail = host.querySelector('[data-ev-detail]');
    detail.className = `ev-detail st-border-${row.status || 'none'}`;
    detail.innerHTML = `${origins.length ? `<div class="ev-pics"><span class="ev-pics-name">原片</span><div>${origins.map(shot => big('orig',shot.number,`镜头 ${shot.number}`)).join('')}</div></div>` : ''}<div class="ev-pics"><span class="ev-pics-name">这次</span><div>${cuts.slice(0,4).map(item => big('cut',item.index,item.code || seconds(item.start,fps))).join('')}${cuts.length > 4 ? `<span class="ev-more">+${cuts.length - 4}</span>` : ''}</div></div><div class="ev-info"><h4>${row.status ? `<span class="ev-status st-${esc(row.status)}">${STATUS[row.status]}</span>` : ''}${esc(title)} <small>${seconds(row.start,fps)}–${seconds(row.end,fps)}</small></h4>${field('原片',row.original)}${field(data.replica ? '这次' : '画面',row.used)}${field('画面',row.picture)}${field(data.replica ? '差在哪' : '说明',row.note)}${spoken.map(line => field(`口播 #${line.number}`,line.zh || line.spoken)).join('')}<div class="ev-actions">${data.timeline_uid ? `<button type="button" class="primary compact" data-ev-open>在达芬奇定位</button>` : ''}<button type="button" class="secondary compact" data-ev-step="-1" ${index ? '' : 'disabled'}>上一个</button><button type="button" class="secondary compact" data-ev-step="1" ${index < data.rows.length - 1 ? '' : 'disabled'}>下一个</button><small>也可以点色条后按 ← →</small></div></div>`;
    detail.querySelectorAll('[data-ev-img]').forEach(image => image.addEventListener('error',() => {image.hidden = true;image.parentElement?.classList.add('ev-no-frame');},{once:true}));
    detail.querySelectorAll('[data-ev-step]').forEach(button => button.onclick = () => showRow(host,data,ctx,index + Number(button.dataset.evStep)));
    const open = detail.querySelector('[data-ev-open]');
    if (open) open.onclick = async () => {
      open.disabled = true;
      try {
        const result = await ctx.api(`/api/production/jobs/${encodeURIComponent(ctx.job.id)}/open`,{method:'POST',body:{unique_id:data.timeline_uid,frame:row.start}});
        ctx.toast(`已在达芬奇打开 ${result.name || '时间线'}，定位到第 ${(row.start / fps).toFixed(2)} 秒。`);
      } catch (error) {ctx.toast(error?.message || '没能在达芬奇定位。',true);}
      finally {open.disabled = false;}
    };
  }

  async function load(details,ctx) {
    const host = details.querySelector('[data-ev-body]'), key = `${ctx.job.id}/${ctx.edit.id}`;
    if (details.dataset.evLoaded) return;
    details.dataset.evLoaded = '1';
    try {
      if (!cache.has(key)) cache.set(key,ctx.api(`${base(ctx.job.id,ctx.edit.id)}/visual`));
      const data = await cache.get(key);
      if (host.isConnected) render(host,data,ctx);
    } catch (error) {
      cache.delete(key);delete details.dataset.evLoaded;
      if (host.isConnected) host.innerHTML = `<p class="ai-error">剪辑说明没读出来：${esc(error?.message || '请稍后重试')}</p>`;
    }
  }

  // The write-up starts folded beside the preview; opening or closing one is remembered for the next order.
  const OPEN_KEY = 'production-edit-visual-open', wantsOpen = () => {try {return localStorage.getItem(OPEN_KEY) === '1';} catch {return false;}};
  // Loads each open write-up now and the folded ones when they are opened.
  function mount(root,{job,api,toast}) {
    root.querySelectorAll('[data-edit-visual]').forEach(details => {
      const edit = (job.edits || []).find(item => item.id === details.dataset.editVisual);
      if (!edit) return;
      const ctx = {job,edit,api,toast};
      if (details.open) void load(details,ctx);
      details.addEventListener('toggle',() => {if (details.open) void load(details,ctx);if (details.isConnected) {try {localStorage.setItem(OPEN_KEY,details.open ? '1' : '0');} catch {}}});
    });
  }

  return {markup,mount,markdown,wantsOpen};
})();
