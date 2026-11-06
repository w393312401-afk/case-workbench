'use strict';

// 挂车图文 for one timeline: upload the no-subtitle export → AI writes 3 per-slide versions → pick one or several,
// made one after another (each version is its own work with its own pictures, style and exports) →
// pick a type style → swap any slide's picture (another frame or an uploaded photo), drag the crop, text position →
// freeze into 图文成品. A slide can be a collage (左右 / 上下 / 四格): click a cell in the preview, and the frame strip,
// upload and crop drag act on that cell. Slides can be copied, deleted and reordered (drag a thumbnail or use the
// move buttons). Swaps only re-lay out the slide; AI is never called for pictures. Under the words, the user can
// write the slide's meaning in Chinese and ask for 3 English wordings in an American creator voice; nothing changes
// until one is picked (pictures always stay), and a pick can be switched or undone.
window.PhotoCarousel = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hot = line => esc(line).replace(/\*([^*]+)\*/g,'<b>$1</b>');
  const setURL = (jobId,uid) => `/api/production/jobs/${encodeURIComponent(jobId)}/photo-set?timeline=${encodeURIComponent(uid)}`;
  const base = id => `/api/photo-sets/${encodeURIComponent(id)}`;
  const frameNo = t => Math.round(Number(t) * 5) + 1;
  const thumb = (set,t,full = false) => `${base(set.id)}/frame/${frameNo(t)}.jpg${full ? '?full=1' : ''}`;
  const rendered = (set,n,width,style = set.work?.style) => `${base(set.id)}/render/${set.work?.key}/${style}/${n}.jpg?w=${width}&v=${encodeURIComponent(set.updated_at || '')}`;
  const STATUS = {done:'已定稿',doing:'正在做',todo:'排队'};
  // Same cell rectangles as carousel_render.LAYOUTS, as fractions of the 3:4 canvas.
  const CELLS = {single:[[0,0,1,1]],lr:[[0,0,.5,1],[.5,0,1,1]],tb:[[0,0,1,.5],[0,.5,1,1]],grid:[[0,0,.5,.5],[.5,0,1,.5],[0,.5,.5,1],[.5,.5,1,1]]};
  const layoutOf = slide => CELLS[slide?.layout] ? slide.layout : 'single';
  // Every cell of a slide as {image,crop_x,crop_y}; cell 1 is the slide's own picture, missing cells copy it (as the server does).
  const cellsOf = slide => {
    const first = {image:slide.image || {},crop_x:slide.crop_x ?? .5,crop_y:slide.crop_y ?? .5};
    const rest = (slide.cells || []).map(cell => ({image:cell.image || first.image,crop_x:cell.crop_x ?? .5,crop_y:cell.crop_y ?? .5}));
    return CELLS[layoutOf(slide)].map((_,index) => index ? rest[index - 1] || {image:first.image,crop_x:.5,crop_y:.5} : first);
  };
  const tokenHeader = () => ({'X-Workbench-Token': (typeof state === 'object' && state.token) || ''});

  const markup = uid => `<div class="photo-carousel-row" data-photo-carousel="${esc(uid)}"><button type="button" class="secondary compact" data-photo-open>做图文</button><span data-photo-status class="context-note" role="status"></span></div>`;

  function statusText(set) {
    if (set?.resolve_import?.status === 'running') return '正在从达芬奇导入无字版…';
    if (!set?.exists) return '';
    if (set.frames_job?.status === 'running') return '正在抽帧…';
    if (set.ai?.status === 'running') return 'AI 正在写逐张表…';
    const works = set.works || [];
    if (works.length) {
      const done = works.filter(work => work.status === 'done').length;
      return done === works.length ? `${done} 版都已定稿` : `${done}/${works.length} 版已定稿，正在做 ${set.work?.key || ''}`;
    }
    if (set.versions?.length) return '逐张表已出，待选版';
    return '已上传视频';
  }

  function upload(file,kind,onProgress) {
    return new Promise((resolve,reject) => {
      const request = new XMLHttpRequest();
      request.open('POST',`/api/upload?kind=${kind}&name=${encodeURIComponent(file.name)}`);
      Object.entries(tokenHeader()).forEach(([k,v]) => request.setRequestHeader(k,v));
      request.setRequestHeader('Content-Type','application/octet-stream');
      request.upload.onprogress = event => onProgress?.(event.lengthComputable ? Math.round(event.loaded / event.total * 100) : null);
      request.onload = () => {
        let result = {};
        try {result = JSON.parse(request.responseText);} catch {}
        if (request.status >= 200 && request.status < 300 && result.path) resolve(result);
        else reject(new Error(result.error || '上传没成功，请重试。'));
      };
      request.onerror = () => reject(new Error('上传中断，请重试。'));
      request.send(file);
    });
  }

  // ---------- dialog ----------
  function open({job,api,uid,timelineName,onChange}) {
    document.querySelector('dialog.photo-carousel-dialog')?.remove();
    const dialog = document.createElement('dialog');
    dialog.className = 'production-dialog photo-carousel-dialog';
    dialog.innerHTML = `<div class="dialog-heading"><div><span class="eyebrow">挂车图文</span><h2>${esc(timelineName || '图文')}</h2></div><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="photo-body" data-photo-body><p class="context-note">正在读取…</p></div>`;
    document.body.append(dialog);dialog.showModal();
    const body = dialog.querySelector('[data-photo-body]');
    const ui = {set:null,frames:null,selected:1,cell:1,zhDraft:{},onlySharp:true,timer:null,busy:false,textTimer:null,pick:null,workKey:null};
    const close = () => {clearTimeout(ui.timer);clearTimeout(ui.textTimer);dialog.close();dialog.remove();onChange?.(ui.set);};
    dialog.querySelector('[data-close]').onclick = close;
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    // The error box sits at the top of a long dialog, so a toast also shows it next to wherever the user is.
    const fail = problem => {
      const message = problem?.message || '操作没完成，请重试。';
      const box = body.querySelector('[data-photo-error]');
      if (box) {box.textContent = message;box.hidden = false;}
      if (typeof toast === 'function') toast(message,true);
    };
    const load = async () => {
      if (!dialog.isConnected) return;
      try {await apply(await api(setURL(job.id,uid)));} catch (problem) {
        fail(problem);
        if (ui.set?.resolve_import?.status === 'running') ui.timer = setTimeout(load,2000);
      }
    };
    const call = async (path,payload = {}) => {
      if (ui.busy) return;
      ui.busy = true;body.classList.add('is-busy');
      try {await apply(await api(path,{method:'POST',body:payload}));}
      catch (problem) {fail(problem);}
      finally {ui.busy = false;body.classList.remove('is-busy');}
    };
    const apply = async set => {
      const previousFrames = ui.set?.frames_job?.status;
      ui.set = set;
      if (set.exists && set.frames_job?.status === 'done' && (!ui.frames || previousFrames !== 'done' || ui.frames.id !== set.id)) {
        try {ui.frames = {...await api(`${base(set.id)}/frames`),id:set.id};} catch {ui.frames = null;}
      }
      if (ui.workKey !== set.work?.key) {ui.selected = 1;ui.workKey = set.work?.key;}
      if (ui.selected > (set.work?.slides || []).length) ui.selected = 1;
      const current = set.work?.slides?.[ui.selected - 1];
      if (!current || ui.cell > CELLS[layoutOf(current)].length) ui.cell = 1;
      draw();
      clearTimeout(ui.timer);
      if (set.resolve_import?.status === 'running' || set.frames_job?.status === 'running' || set.ai?.status === 'running' || set.slide_ai?.status === 'running') ui.timer = setTimeout(load,2000);
    };

    // ----- sections -----
    const videoSection = set => {
      const task = set.resolve_import || {}, running = task.status === 'running';
      const button = `<button type="button" class="${set.exists ? 'secondary' : 'primary'} compact" data-photo-resolve ${running || set.ai?.status === 'running' ? 'disabled' : ''} aria-busy="${running}">${running ? '导出并导入中…' : '从达芬奇导入无字版'}</button>`;
      const percent = Number.isFinite(task.percent) ? ` ${Math.max(0,Math.min(100,task.percent))}%` : '';
      const feedback = task.status === 'failed' ? `<p class="ai-error photo-resolve-note" data-photo-resolve-error role="alert">${esc(task.error)}</p>` : task.message ? `<p class="context-note photo-resolve-note" data-photo-resolve-progress role="status">${esc(task.message)}${percent}</p>` : '';
      const drop = `<label class="photo-drop" data-photo-drop><input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm" hidden data-photo-video><span>${set.exists ? '换一个视频：' : '也可以'}把<strong>关掉字幕轨和屏显轨</strong>导出的视频拖到这里，或点这里选文件</span><small data-photo-upload-progress></small></label>`;
      if (running) return `<section class="photo-step"><h3><i>1</i>视频</h3><div class="photo-step-line">${button}</div>${feedback}</section>`;
      if (!set.exists) return `<section class="photo-step"><h3><i>1</i>视频</h3><div class="photo-step-line photo-resolve-actions">${button}</div>${feedback}${drop}</section>`;
      const job = set.frames_job || {};
      const info = job.status === 'running' ? '正在抽帧…' : job.status === 'failed' ? `<span class="ai-error">${esc(job.error)}</span>` : `${esc(set.video?.name)} · ${esc(set.video?.duration)} 秒 · ${esc(job.count)} 帧`;
      return `<section class="photo-step is-compact"><h3><i>1</i>视频</h3><div class="photo-step-line"><span>${info}</span>${button}<details class="photo-replace"><summary>换视频</summary>${drop}</details></div>${feedback}</section>`;
    };
    const planSection = set => {
      if (!set.exists || set.frames_job?.status !== 'done') return '';
      const ai = set.ai || {};
      if (ai.status === 'running') return `<section class="photo-step"><h3><i>2</i>文案</h3><div class="photo-step-line"><span class="context-note">${esc(ai.progress || 'AI 正在写…')}</span><button type="button" class="secondary compact" data-photo-cancel>停止</button></div></section>`;
      const ask = `<div class="photo-step-line"><input type="text" maxlength="1000" data-photo-note placeholder="补充说明（可选）：今天挂车价、活动、想强调的点…"><button type="button" class="${set.versions?.length ? 'secondary' : 'primary'} compact" data-photo-plan>${set.versions?.length ? '重新出 3 版' : 'AI 出 3 版逐张表'}</button></div>${ai.status === 'failed' || ai.status === 'cancelled' ? `<p class="ai-error">${esc(ai.error)}</p>` : ''}`;
      if (!set.versions?.length) return `<section class="photo-step"><h3><i>2</i>文案</h3>${ask}</section>`;
      const queued = Object.fromEntries((set.works || []).map(work => [work.key,work]));
      if (!ui.pick) ui.pick = new Set(Object.keys(queued).length ? [] : set.versions.filter(version => version.recommended).map(version => version.key));
      const cards = set.versions.map(version => `<article class="photo-version ${queued[version.key] || ui.pick.has(version.key) ? 'is-chosen' : ''}">
        <header><strong>${esc(version.key)} · ${esc(version.name_zh)}</strong>${version.writing && set.writings?.[version.writing] ? `<span class="photo-chip is-writing">${esc(set.writings[version.writing])}</span>` : ''}${version.recommended ? '<span class="photo-chip">推荐</span>' : ''}${queued[version.key] ? `<span class="photo-chip is-${esc(queued[version.key].status)}">${STATUS[queued[version.key].status]}</span>` : `<label class="photo-pick"><input type="checkbox" data-photo-pick="${esc(version.key)}" ${ui.pick.has(version.key) ? 'checked' : ''}>做这版</label>`}</header>
        <p class="photo-meta">卖给：${esc(version.buyer_zh)}</p>${version.motive_zh ? `<p class="photo-meta">${esc(version.motive_zh)}</p>` : ''}
        <ol>${version.slides.map((slide,index) => `<li><img loading="lazy" src="${thumb(set,slide.frame_time)}" alt=""><div><p class="photo-title">${slide.title_lines.map(hot).join(' / ')}</p><p class="photo-fact">${esc((slide.body_lines || []).map(line => line.replaceAll('*','')).filter(line => line !== (version.cta || '').replaceAll('*','')).join(' / '))}</p><p class="photo-zh">${esc(slide.title_zh)}｜${esc(slide.body_zh)}</p><small>${esc(slide.frame_time)} 秒${layoutOf(slide) !== 'single' ? ` · 拼图${esc(set.layouts?.[slide.layout] || '')}` : ''} · ${esc(slide.picture_zh)}</small></div></li>`).join('')}</ol>
        ${version.cta ? `<p class="photo-fact">收口：${hot(version.cta)}</p>` : ''}
      </article>`).join('');
      const gaps = set.gaps?.length ? `<details class="photo-gaps"><summary>缺口与没写的说法（${set.gaps.length}）</summary><ul>${set.gaps.map(gap => `<li>${esc(gap)}</li>`).join('')}</ul></details>` : '';
      const picked = set.versions.filter(version => ui.pick.has(version.key) && !queued[version.key]).map(version => version.key);
      const add = picked.length ? `<div class="photo-step-line photo-pick-bar"><span>${(set.works || []).length ? '加进制作队列，排在后面' : '按顺序做'}：${picked.join(' → ')}</span><button type="button" class="primary compact" data-photo-choose>${(set.works || []).length ? `再做这 ${picked.length} 版` : `开始做这 ${picked.length} 版`}</button></div>` : '';
      return `<section class="photo-step"><h3><i>2</i>文案<small>可以选一版、两版或三版，按顺序一版一版做</small></h3><div class="photo-versions">${cards}</div>${add}${gaps}<details class="photo-replan"><summary>重新出 3 版</summary>${ask}</details></section>`;
    };
    const queueSection = set => {
      const works = set.works || [];
      if (!works.length) return '';
      const chips = works.map((work,index) => `${index ? '<span class="photo-arrow">→</span>' : ''}<span class="photo-queue-item ${work.key === set.work?.key ? 'is-current' : ''} is-${esc(work.status)}"><button type="button" data-photo-work="${esc(work.key)}" ${work.openable && work.key !== set.work?.key ? '' : 'disabled'} title="${work.openable ? '' : '按顺序做：先把前面的版本定稿'}">${esc(work.key)} ${esc(work.name_zh)} · ${STATUS[work.status]}</button>${work.exports ? '' : `<button type="button" class="photo-queue-remove" data-photo-remove="${esc(work.key)}" aria-label="把 ${esc(work.key)} 移出队列" title="移出队列">×</button>`}</span>`).join('');
      return `<div class="photo-queue" role="group" aria-label="制作队列"><span>制作队列</span>${chips}</div>`;
    };
    const styleSection = set => {
      const w = set.work;
      if (!w?.slides?.length) return '';
      const styles = Object.entries(set.styles || {}).map(([key,label]) => `<button type="button" class="${w.style === key ? 'is-current' : ''}" data-photo-style="${esc(key)}" aria-pressed="${w.style === key}">${esc(label)}</button>`).join('');
      const total = w.slides.length, n = ui.selected, full = total >= (set.max_slides || 8);
      const strip = w.slides.map((slide,index) => `<button type="button" class="photo-slide ${n === index + 1 ? 'is-selected' : ''}" data-photo-select="${index + 1}" draggable="true" aria-label="第 ${index + 1} 张" title="点一下编辑这一张；按住拖到别的位置调整顺序"><img src="${rendered(set,index + 1,270)}" alt="" draggable="false"><span class="photo-slide-no">${index + 1}</span></button>`).join('');
      const tools = `<div class="photo-step-line photo-slide-tools"><span>第 ${n} 张</span>
        <button type="button" class="subtle compact" data-photo-arrange="duplicate" ${full ? `disabled title="一组最多 ${set.max_slides || 8} 张"` : ''}>复制这一张</button>
        <button type="button" class="subtle compact" data-photo-arrange="delete" ${total <= 1 ? 'disabled title="至少要留一张"' : ''}>删掉这一张</button>
        <button type="button" class="subtle compact" data-photo-arrange="back" ${n <= 1 ? 'disabled' : ''} aria-label="把第 ${n} 张往前挪一位">← 前移</button>
        <button type="button" class="subtle compact" data-photo-arrange="forward" ${n >= total ? 'disabled' : ''} aria-label="把第 ${n} 张往后挪一位">后移 →</button></div>`;
      return `<section class="photo-step"><h3><i>3</i>排版<span class="photo-segment">${styles}</span><span class="photo-history"><button type="button" class="subtle compact" data-photo-undo ${w.can_undo ? `title="撤销：${esc(w.undo_label)}"` : 'disabled'}>撤销上一步</button><button type="button" class="subtle compact" data-photo-redo ${w.can_redo ? `title="恢复：${esc(w.redo_label)}"` : 'disabled'}>恢复上一步</button></span></h3><div class="photo-strip" data-photo-strip>${strip}</div>${tools}</section>`;
    };
    const editSection = set => {
      const w = set.work;
      if (!w?.slides?.length) return '';
      const n = ui.selected, slide = w.slides[n - 1];
      const layout = layoutOf(slide), cells = cellsOf(slide), multi = cells.length > 1;
      const image = cells[ui.cell - 1].image || {};
      const frames = (ui.frames?.items || []);
      const sharpCut = (() => {const values = frames.map(item => item.sharp).sort((a,b) => a - b);return values[Math.floor(values.length * .4)] || 0;})();
      const shown = ui.onlySharp ? frames.filter(item => item.sharp >= sharpCut || (image.kind === 'frame' && Math.abs(item.t - image.t) < 1e-6)) : frames;
      const strip = shown.map(item => `<button type="button" class="photo-frame ${image.kind === 'frame' && Math.abs(item.t - image.t) < 1e-6 ? 'is-current' : ''}" data-photo-frame="${item.t}" title="${item.t} 秒"><img loading="lazy" src="${base(set.id)}/frame/${item.n}.jpg" alt=""><span>${item.t.toFixed(1)}</span></button>`).join('');
      const positions = Object.entries(set.positions || {}).map(([key,label]) => `<button type="button" class="${(slide.text_position || 'split') === key ? 'is-current' : ''}" data-photo-position="${esc(key)}">${esc(label)}</button>`).join('');
      const sizes = Object.entries(set.sizes || {}).map(([key,label]) => `<button type="button" class="${(slide.text_size || 'l') === key ? 'is-current' : ''}" data-photo-size="${esc(key)}">${esc(label)}</button>`).join('');
      const layouts = Object.entries(set.layouts || {}).map(([key,label]) => `<button type="button" class="${layout === key ? 'is-current' : ''}" data-photo-layout="${esc(key)}" aria-pressed="${layout === key}">${esc(label)}</button>`).join('');
      const describe = picture => picture.kind === 'upload' ? `上传图：${esc(picture.name)}` : `视频 ${esc(picture.t)} 秒`;
      const source = multi ? cells.map((cell,index) => `${index + 1}. ${describe(cell.image || {})}`).join('　') : describe(image);
      const cellBoxes = CELLS[layout].map(([x0,y0,x1,y1],index) => `<div class="photo-cell ${multi && index + 1 === ui.cell ? 'is-active' : ''}" data-photo-cell="${index + 1}" data-rect="${x0},${y0},${x1},${y1}">
            <img class="photo-crop-raw" src="${base(set.id)}/base/${w.key}/${n}?cell=${index + 1}&v=${encodeURIComponent(JSON.stringify(cells[index].image || {}))}" alt="" draggable="false" data-pos="${cells[index].crop_x},${cells[index].crop_y}">
            ${multi ? `<span class="photo-cell-no">${index + 1}</span>` : ''}</div>`).join('');
      const last = n === w.slides.length;
      const task = set.slide_ai || {}, rewriting = task.status === 'running', mine = task.key === w.key && (task.slide_id ? slide.id === task.slide_id : task.number === n);
      const zhNow = [slide.title_zh,slide.body_zh].filter(Boolean).join('\n');
      // What the user typed as the meaning is theirs: undo, redo, a pick or a poll redraw never replace it.
      const draftKey = `${w.key}:${n}`;
      const zhValue = ui.zhDraft[draftKey] ?? (mine && task.zh ? task.zh : zhNow);
      const same = (a,b) => JSON.stringify(a || []) === JSON.stringify(b || []);
      const inUse = option => same(option.title_lines,slide.title_lines) && same(option.body_lines,slide.body_lines);
      const options = mine && task.status === 'done' ? (task.options || []).map((option,index) => `<button type="button" class="photo-option ${inUse(option) ? 'is-current' : ''}" data-photo-pick="${index}" aria-pressed="${inUse(option)}">
          ${option.label ? `<span class="photo-option-kind">${esc(option.label)}</span>` : ''}
          <span class="photo-title">${(option.title_lines || []).map(hot).join(' / ')}</span>
          ${(option.body_lines || []).length ? `<span class="photo-fact">${esc(option.body_lines.join(' / '))}</span>` : ''}
          <span class="photo-zh">${esc([option.title_zh,option.body_zh].filter(Boolean).join('｜'))}</span>
          ${option.change_zh ? `<small>${esc(option.change_zh)}</small>` : ''}${inUse(option) ? '<em>已用</em>' : ''}</button>`).join('') : '';
      const rewrite = `<div class="photo-rewrite">
          <label class="photo-control is-text"><span>中文意思（改这里，AI 按美式达人口吻出 3 种英文）</span><textarea rows="2" data-photo-zh ${rewriting && mine ? 'disabled' : ''}>${esc(zhValue)}</textarea></label>
          ${rewriting
            ? `<div class="photo-step-line"><span class="context-note" role="status">${mine ? esc(task.progress || `AI 正在给第 ${n} 张出 3 种说法…`) : `AI 正在给第 ${esc(task.number)} 张出说法…`}</span><button type="button" class="secondary compact" data-photo-rewrite-stop>停止</button></div>`
            : `<div class="photo-step-line"><input type="text" maxlength="500" data-photo-rewrite-note placeholder="其他要求（可选）：短一点、讲质保…" value="${mine ? esc(task.note || '') : ''}"><button type="button" class="secondary compact" data-photo-rewrite>${options ? '再出 3 种' : '出 3 种说法'}</button></div>`}
          ${options ? `<div class="photo-options" role="group" aria-label="AI 给的 3 种说法">${options}</div><p class="photo-hint">点一种换上；不满意可以换另一种，或点「撤销上一步」。</p>` : ''}
          ${mine && (task.status === 'failed' || task.status === 'cancelled') ? `<p class="ai-error" data-photo-rewrite-error>${esc(task.error)}</p>` : ''}</div>`;
      const locked = rewriting && mine ? 'disabled title="AI 正在给这一张出说法"' : '';
      return `<section class="photo-step photo-editor"><h3><i>4</i>第 ${n} 张 <small>${source}</small></h3>
        <div class="photo-editor-grid"><div class="photo-crop ${multi ? 'is-collage' : ''}" data-photo-crop tabindex="0" role="slider" aria-label="${multi ? `第 ${ui.cell} 格：拖动调整裁切位置` : '拖动调整裁切位置'}" aria-valuetext="上下 ${Math.round(cells[ui.cell - 1].crop_y * 100)}%，左右 ${Math.round(cells[ui.cell - 1].crop_x * 100)}%" title="${multi ? '点一格选中它，再按住拖动调整这一格的裁切；方向键微调' : '按住图片上下左右拖动，调整裁出来的部分；方向键微调'}">
          <img class="photo-preview" src="${rendered(set,n,540)}" alt="第 ${n} 张预览" draggable="false">
          ${cellBoxes}
          <span class="photo-crop-tip">${multi ? `点格子选中，拖动调第 ${ui.cell} 格` : '按住拖动调整裁切'}</span></div>
        <div class="photo-controls">
          <div class="photo-control"><span>版式</span><span class="photo-segment" aria-label="版式">${layouts}</span></div>
          <div class="photo-control"><span>${multi ? `换第 ${ui.cell} 格` : '换图'}</span><label class="photo-check"><input type="checkbox" data-photo-sharp ${ui.onlySharp ? 'checked' : ''}>只看清楚的帧</label><label class="secondary compact photo-upload-button">上传图片<input type="file" accept="image/png,image/jpeg,image/webp" hidden data-photo-image></label><small data-photo-image-progress></small></div>
          <div class="photo-frames" data-photo-frames role="group" aria-label="可选的帧">${strip || '<span class="context-note">没有抽帧</span>'}</div>
          <div class="photo-control"><span>文字位置</span><span class="photo-segment">${positions}</span><span class="photo-segment" aria-label="字号">${sizes}</span></div>
          <p class="photo-hint">${multi ? '拼图只用在前后对比或套装；每格对上它那一半文案，字放在某一格的空处，上下拼图可以放中间压在接缝上。' : '字放在桌面、背景这类空处，别挡刀、磨刀架和箱子；空处不够先挪裁切或缩字，再不行就换一帧。'}</p>
          <label class="photo-control is-text"><span>标题（一行一句，*星号* 包住的词变色）</span><textarea rows="2" data-photo-title ${locked}>${esc((slide.title_lines || []).join('\n'))}</textarea></label>
          <label class="photo-control is-text"><span>第二行（一句口语，最多排两行）</span><textarea rows="2" data-photo-body-lines ${locked}>${esc((slide.body_lines || []).join('\n'))}</textarea></label>
          ${last ? `<label class="photo-control is-text"><span>收口（末张）</span><input type="text" maxlength="60" value="${esc(w.cta || '')}" data-photo-cta></label>` : ''}
          ${rewrite}
        </div></div></section>`;
    };
    // Same list as the video's 成片与发布: three 🔴 title lines, then the text. Each line shows the English with its
    // Chinese under it; a title copies its English alone, the text copies Chinese, a blank line, then English.
    const publishCopy = w => {
      const copyIcon = typeof icon === 'function' ? icon('copy') : '';
      const flat = value => String(value || '').replace(/\s*\n\s*/g,' ');
      const item = (attributes,main,under,hint) => `<button type="button" class="ai-title-line video-publish-copy-item" ${attributes} title="${esc(hint)}"><span><span class="video-publish-copy-main">${esc(main)}</span>${under ? `<small>${esc(under)}</small>` : ''}</span>${copyIcon}</button>`;
      const titles = (w.titles || []).map((line,index) => item(`data-photo-copy-title="${index}"`,line,w.titles_zh?.[index] || '',line)).join('');
      const text = w.caption_en || w.caption_zh ? item('data-photo-copy-text',flat(w.caption_en || w.caption_zh),w.caption_en ? flat(w.caption_zh) : '','复制中文和外文文案') : '';
      const line = (label,content) => content ? `<span class="video-publish-copy-label">${label}</span><div class="video-publish-copy-lines">${content}</div>` : '';
      return `<div class="photo-publish-copy"><h4>发布文案 <small>点一下复制</small></h4>
        ${titles || text ? `<div class="video-publish-copy-row">${line('标题',titles)}${line('文案',text)}</div>` : '<p class="context-note">这一版还没有发布文案，可以在下面写。</p>'}
        <details class="photo-caption"><summary>改发布文案</summary>
          <label class="field"><span>标题（一行一条，最多 3 条，开头自动加 🔴）</span><textarea rows="3" maxlength="900" data-photo-titles>${esc((w.titles || []).join('\n'))}</textarea></label>
          <label class="field"><span>英文文案</span><textarea rows="3" maxlength="1200" data-photo-caption-en>${esc(w.caption_en)}</textarea></label>
          <label class="field"><span>中文对照</span><textarea rows="2" maxlength="800" data-photo-caption-zh>${esc(w.caption_zh)}</textarea></label></details></div>`;
    };
    const exportSection = set => {
      const w = set.work;
      if (!w?.slides?.length) return '';
      const exports = (w.exports || []).slice().reverse().map(item => `<li><span>${esc(item.name)}</span><button type="button" class="subtle compact" data-photo-folder="${esc(item.name)}">打开文件夹</button></li>`).join('');
      return `<section class="photo-step"><h3><i>5</i>定稿</h3>
        ${publishCopy(w)}
        <div class="photo-step-line"><span class="context-note">把第 ${esc(w.key)} 版按当前排版导出到「图文成品」新文件夹（发布 JPG＋无损 PNG＋copy.json），旧版本不动。</span><button type="button" class="primary compact" data-photo-export>${w.status === 'done' ? '再导出一次' : '定稿导出'}</button>${set.next ? `<button type="button" class="primary compact" data-photo-work="${esc(set.next.key)}">做下一版：${esc(set.next.key)} ${esc(set.next.name_zh)}</button>` : ''}</div>
        ${exports ? `<ul class="photo-exports">${exports}</ul>` : ''}</section>`;
    };

    function draw() {
      const set = ui.set;
      const scroll = body.scrollTop, frameScroll = body.querySelector('[data-photo-frames]')?.scrollTop;
      const focused = document.activeElement;
      const typing = focused && body.contains(focused) && focused.matches('textarea,input[type=text]')
        ? {name:[...focused.attributes].find(attr => attr.name.startsWith('data-photo'))?.name,value:focused.value,start:focused.selectionStart,end:focused.selectionEnd} : null;
      const importing = set.resolve_import?.status === 'running';
      body.innerHTML = `<p class="ai-error" data-photo-error role="alert" hidden></p>${videoSection(set)}${importing ? '' : `${planSection(set)}${queueSection(set)}${styleSection(set)}${editSection(set)}${exportSection(set)}`}`;
      // The page's security policy drops style="" written into markup, so cell boxes and crop positions are set here.
      body.querySelectorAll('[data-photo-cell][data-rect]').forEach(node => {
        const [x0,y0,x1,y1] = node.dataset.rect.split(',').map(Number);
        Object.assign(node.style,{left:`${x0 * 100}%`,top:`${y0 * 100}%`,width:`${(x1 - x0) * 100}%`,height:`${(y1 - y0) * 100}%`});
      });
      body.querySelectorAll('.photo-crop-raw[data-pos]').forEach(image => {
        const [x,y] = image.dataset.pos.split(',').map(Number);
        image.style.objectPosition = `${x * 100}% ${y * 100}%`;
      });
      body.scrollTop = scroll;
      // Focus before restoring the text: the browser then still sees it as typed and saves it when the field is left.
      if (typing?.name) {
        const node = body.querySelector(`[${typing.name}]`);
        if (node && !node.disabled) {node.focus();node.value = typing.value;try {node.setSelectionRange(typing.start,typing.end);} catch {}}
      }
      const frameBox = body.querySelector('[data-photo-frames]');
      if (frameBox) {
        if (frameScroll != null) frameBox.scrollTop = frameScroll;
        else {
          const current = frameBox.querySelector('.is-current');
          if (current) frameBox.scrollTop = current.offsetTop - frameBox.offsetTop - (frameBox.clientHeight - current.offsetHeight) / 2;
        }
      }
      bind();
    }

    function bind() {
      const set = ui.set, n = ui.selected;
      const fromResolve = body.querySelector('[data-photo-resolve]');
      if (fromResolve) fromResolve.onclick = () => call(`/api/production/jobs/${encodeURIComponent(job.id)}/photo-set/from-resolve`,{timeline_uid:uid});
      body.querySelectorAll('[data-photo-video]').forEach(input => input.onchange = () => input.files[0] && sendVideo(input.files[0]));
      body.querySelectorAll('[data-photo-drop]').forEach(zone => {
        zone.ondragover = event => {event.preventDefault();zone.classList.add('is-over');};
        zone.ondragleave = () => zone.classList.remove('is-over');
        zone.ondrop = event => {event.preventDefault();zone.classList.remove('is-over');const file = event.dataTransfer.files[0];if (file) sendVideo(file);};
      });
      const plan = body.querySelector('[data-photo-plan]');
      if (plan) plan.onclick = () => call(`${base(set.id)}/plan`,{note:body.querySelector('[data-photo-note]')?.value || ''});
      const cancel = body.querySelector('[data-photo-cancel]');
      if (cancel) cancel.onclick = () => call(`${base(set.id)}/cancel`);
      body.querySelectorAll('[data-photo-pick]').forEach(box => box.onchange = () => {
        if (box.checked) ui.pick.add(box.dataset.photoPick); else ui.pick.delete(box.dataset.photoPick);
        draw();
      });
      const choose = body.querySelector('[data-photo-choose]');
      if (choose) choose.onclick = async () => {
        const keys = set.versions.map(version => version.key).filter(key => ui.pick.has(key) && !(set.works || []).some(work => work.key === key));
        await call(`${base(set.id)}/choose`,{versions:keys});
        ui.pick = new Set();draw();
      };
      body.querySelectorAll('[data-photo-work]').forEach(button => button.onclick = () => call(`${base(set.id)}/work`,{key:button.dataset.photoWork}));
      body.querySelectorAll('[data-photo-remove]').forEach(button => button.onclick = () => call(`${base(set.id)}/remove`,{key:button.dataset.photoRemove}));
      body.querySelectorAll('[data-photo-style]').forEach(button => button.onclick = () => call(`${base(set.id)}/style`,{style:button.dataset.photoStyle}));
      body.querySelectorAll('[data-photo-select]').forEach(button => button.onclick = () => {ui.selected = Number(button.dataset.photoSelect);ui.cell = 1;draw();});
      // Copy / delete / move: after the server saved it, keep editing the slide the user is looking at.
      const arrange = async (number,payload,select) => {
        const before = ui.set.updated_at;
        await call(`${base(set.id)}/slides/${number}/arrange`,payload);
        if (ui.set.updated_at !== before) {ui.selected = Math.min(select,ui.set.work?.slides?.length || 1);ui.cell = 1;draw();}
      };
      body.querySelectorAll('[data-photo-arrange]').forEach(button => button.onclick = () => {
        const kind = button.dataset.photoArrange;
        if (kind === 'duplicate') arrange(n,{action:'duplicate'},n + 1);
        else if (kind === 'delete') arrange(n,{action:'delete'},n);
        else {const to = kind === 'back' ? n - 1 : n + 1;arrange(n,{action:'move',to},to);}
      });
      body.querySelectorAll('[data-photo-strip] [data-photo-select]').forEach(button => {
        button.ondragstart = event => {ui.dragFrom = Number(button.dataset.photoSelect);event.dataTransfer.effectAllowed = 'move';event.dataTransfer.setData('text/plain',String(ui.dragFrom));button.classList.add('is-dragged');};
        button.ondragend = () => {ui.dragFrom = null;body.querySelectorAll('.photo-slide').forEach(node => node.classList.remove('is-dragged','is-drop-target'));};
        button.ondragover = event => {if (!ui.dragFrom) return;event.preventDefault();event.dataTransfer.dropEffect = 'move';button.classList.add('is-drop-target');};
        button.ondragleave = () => button.classList.remove('is-drop-target');
        button.ondrop = event => {
          event.preventDefault();
          const from = ui.dragFrom || Number(event.dataTransfer.getData('text/plain')), to = Number(button.dataset.photoSelect);
          ui.dragFrom = null;
          if (from && to && from !== to) arrange(from,{action:'move',to},to);
          else draw();
        };
      });
      const undo = body.querySelector('[data-photo-undo]');
      if (undo) undo.onclick = () => call(`${base(set.id)}/undo`);
      const redo = body.querySelector('[data-photo-redo]');
      if (redo) redo.onclick = () => call(`${base(set.id)}/redo`);
      const slideCall = payload => call(`${base(set.id)}/slides/${n}`,payload);
      const cellCall = payload => slideCall(ui.cell > 1 ? {...payload,cell:ui.cell} : payload);
      body.querySelectorAll('[data-photo-frame]').forEach(button => button.onclick = () => cellCall({image:{kind:'frame',t:Number(button.dataset.photoFrame)}}));
      body.querySelectorAll('[data-photo-layout]').forEach(button => button.onclick = () => {
        if (button.dataset.photoLayout === layoutOf(set.work.slides[n - 1])) return;
        ui.cell = 1;slideCall({layout:button.dataset.photoLayout});
      });
      const sharp = body.querySelector('[data-photo-sharp]');
      if (sharp) sharp.onchange = () => {ui.onlySharp = sharp.checked;draw();};
      const image = body.querySelector('[data-photo-image]');
      if (image) image.onchange = async () => {
        const file = image.files[0];if (!file) return;
        const note = body.querySelector('[data-photo-image-progress]');
        try {const staged = await upload(file,'screenshot',value => {if (note) note.textContent = value == null ? '上传中…' : `上传 ${value}%`;});
          await cellCall({image:{kind:'upload',path:staged.path,name:file.name}});}
        catch (problem) {fail(problem);}
      };
      const crop = body.querySelector('[data-photo-crop]');
      if (crop) bindCrop(crop,cellsOf(set.work.slides[n - 1]),cellCall);
      body.querySelectorAll('[data-photo-position]').forEach(button => button.onclick = () => slideCall({text_position:button.dataset.photoPosition}));
      body.querySelectorAll('[data-photo-size]').forEach(button => button.onclick = () => slideCall({text_size:button.dataset.photoSize}));
      const rewrite = body.querySelector('[data-photo-rewrite]');
      if (rewrite) rewrite.onclick = () => {
        // Unchanged Chinese means "same meaning, say it better"; changed Chinese is the new meaning to write from.
        const slide = set.work.slides[n - 1], zh = (body.querySelector('[data-photo-zh]')?.value || '').trim();
        const current = [slide.title_zh,slide.body_zh].filter(Boolean).join('\n').trim();
        call(`${base(set.id)}/slides/${n}/rewrite`,{note:body.querySelector('[data-photo-rewrite-note]')?.value || '',zh:zh === current ? '' : zh});
      };
      const zhField = body.querySelector('[data-photo-zh]');
      if (zhField) zhField.oninput = () => {ui.zhDraft[`${set.work.key}:${n}`] = zhField.value;};
      body.querySelectorAll('[data-photo-pick]').forEach(button => button.onclick = () => call(`${base(set.id)}/slides/${n}/pick`,{index:Number(button.dataset.photoPick)}));
      const stopRewrite = body.querySelector('[data-photo-rewrite-stop]');
      if (stopRewrite) stopRewrite.onclick = () => call(`${base(set.id)}/rewrite-stop`);
      const textSave = (node,key,split = true) => {
        if (!node) return;
        node.onchange = () => slideCall({[key]:split ? node.value.split('\n') : node.value});
      };
      textSave(body.querySelector('[data-photo-title]'),'title_lines');
      textSave(body.querySelector('[data-photo-body-lines]'),'body_lines');
      const cta = body.querySelector('[data-photo-cta]');
      if (cta) cta.onchange = () => call(`${base(set.id)}/copy`,{cta:cta.value});
      const en = body.querySelector('[data-photo-caption-en]'), zh = body.querySelector('[data-photo-caption-zh]');
      if (en) en.onchange = () => call(`${base(set.id)}/copy`,{caption_en:en.value});
      if (zh) zh.onchange = () => call(`${base(set.id)}/copy`,{caption_zh:zh.value});
      const titlesField = body.querySelector('[data-photo-titles]');
      if (titlesField) titlesField.onchange = () => call(`${base(set.id)}/copy`,{titles:titlesField.value.split('\n')});
      const write = (button,text,message) => {
        if (typeof copyText === 'function') copyText(text,message); else navigator.clipboard?.writeText(text);
        button.classList.add('copied');setTimeout(() => button.classList.remove('copied'),1200);
      };
      body.querySelectorAll('[data-photo-copy-title]').forEach(button => button.onclick = () => write(button,set.work.titles[Number(button.dataset.photoCopyTitle)],'标题与主题标签已复制'));
      const textCopy = body.querySelector('[data-photo-copy-text]');
      if (textCopy) textCopy.onclick = () => write(textCopy,[set.work.caption_zh,set.work.caption_en].filter(Boolean).join('\n\n'),set.work.caption_zh && set.work.caption_en ? '中英文案已复制' : '文案已复制');
      const exporter = body.querySelector('[data-photo-export]');
      if (exporter) exporter.onclick = async () => {
        if (ui.busy) return;
        exporter.disabled = true;exporter.textContent = '正在导出…';
        try {const result = await api(`${base(set.id)}/export`,{method:'POST',body:{}});apply(result);
          if (typeof toast === 'function') toast(`已导出到 图文成品/${result.export?.name}`);}
        catch (problem) {fail(problem);exporter.disabled = false;exporter.textContent = '定稿导出';}
      };
      body.querySelectorAll('[data-photo-folder]').forEach(button => button.onclick = async () => {
        try {await api(`${base(set.id)}/open`,{method:'POST',body:{name:button.dataset.photoFolder}});} catch (problem) {fail(problem);}
      });
    }

    // Drag the picture inside its window (the whole 3:4 slide, or the selected cell of a collage). While dragging the
    // raw photo is shown (same cover/position rule as the renderer), the text-laid preview comes back after the drop is
    // saved. On a collage, pressing another cell selects it instead of dragging.
    function bindCrop(box,cells,save) {
      const active = box.querySelector(`[data-photo-cell="${ui.cell}"]`) || box.querySelector('[data-photo-cell]');
      const raw = active.querySelector('.photo-crop-raw'), cell = cells[ui.cell - 1] || cells[0];
      let pos = {x:cell.crop_x,y:cell.crop_y}, drag = null;
      const clamp = value => Math.min(1,Math.max(0,value));
      const paint = () => {raw.style.objectPosition = `${pos.x * 100}% ${pos.y * 100}%`;};
      const overflow = () => {
        const width = active.clientWidth, height = active.clientHeight, iw = raw.naturalWidth || 1, ih = raw.naturalHeight || 1;
        const scale = Math.max(width / iw,height / ih);
        return {x:iw * scale - width,y:ih * scale - height};
      };
      const commit = () => {
        box.classList.remove('is-dragging');
        const changed = Math.abs(pos.x - cell.crop_x) > .002 || Math.abs(pos.y - cell.crop_y) > .002;
        if (changed) save({crop_y:Math.round(pos.y * 1000) / 1000,crop_x:Math.round(pos.x * 1000) / 1000});
      };
      box.onpointerdown = event => {
        if (event.button !== 0) return;
        const pressed = event.target.closest?.('[data-photo-cell]');
        if (cells.length > 1 && pressed && Number(pressed.dataset.photoCell) !== ui.cell) {
          event.preventDefault();ui.cell = Number(pressed.dataset.photoCell);draw();
          body.querySelector('[data-photo-crop]')?.focus();
          return;
        }
        event.preventDefault();box.setPointerCapture(event.pointerId);
        drag = {x:event.clientX,y:event.clientY,start:{...pos},room:overflow()};
        box.classList.add('is-dragging');paint();
      };
      box.onpointermove = event => {
        if (!drag) return;
        // Moving the picture down shows more of its top, so the window moves up.
        if (drag.room.x > 1) pos.x = clamp(drag.start.x - (event.clientX - drag.x) / drag.room.x);
        if (drag.room.y > 1) pos.y = clamp(drag.start.y - (event.clientY - drag.y) / drag.room.y);
        paint();
      };
      box.onpointerup = box.onpointercancel = () => {if (!drag) return;drag = null;commit();};
      box.onkeydown = event => {
        const step = event.shiftKey ? .1 : .02;
        const moves = {ArrowUp:['y',-step],ArrowDown:['y',step],ArrowLeft:['x',-step],ArrowRight:['x',step]};
        if (!moves[event.key]) return;
        event.preventDefault();
        const [axis,delta] = moves[event.key];
        pos[axis] = clamp(pos[axis] + delta);
        box.classList.add('is-dragging');paint();
        clearTimeout(box._cropTimer);box._cropTimer = setTimeout(commit,600);
      };
    }

    async function sendVideo(file) {
      if (ui.busy || ui.set?.resolve_import?.status === 'running') return;
      ui.busy = true;body.classList.add('is-busy');
      const note = body.querySelector('[data-photo-upload-progress]');
      try {
        const staged = await upload(file,'video',value => {if (note) note.textContent = value == null ? '上传中…' : `上传 ${value}%`;});
        if (note) note.textContent = '正在抽帧…';
        ui.frames = null;
        await apply(await api(`/api/production/jobs/${encodeURIComponent(job.id)}/photo-set`,{method:'POST',body:{timeline_uid:uid,video_path:staged.path,video_name:file.name}}));
      } catch (problem) {fail(problem);if (note) note.textContent = '';}
      finally {ui.busy = false;body.classList.remove('is-busy');}
    }

    void load();
  }

  async function bind(host,{job,api,uid} = {}) {
    const root = host.querySelector('[data-photo-carousel]');if (!root || !uid) return;
    const status = root.querySelector('[data-photo-status]'), button = root.querySelector('[data-photo-open]');
    const timeline = (job.timelines || []).find(row => row.unique_id === uid);
    const show = set => {
      if (!root.isConnected) return;
      status.textContent = statusText(set);
      button.textContent = set?.exists ? '继续做图文' : '做图文';
    };
    button.onclick = () => open({job,api,uid,timelineName:timeline?.name,onChange:show});
    try {show(await api(setURL(job.id,uid)));} catch {status.textContent = '';}
  }
  return {markup,bind};
})();
