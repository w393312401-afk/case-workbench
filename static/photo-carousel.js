'use strict';

// 挂车图文 for one timeline: upload the no-subtitle export → AI writes 3 per-slide versions → pick one or several,
// made one after another (each version is its own work with its own pictures, style and exports) →
// pick a type style → swap any slide's picture (another frame or an uploaded photo), drag the crop, text position →
// freeze into 图文成品. Swaps only re-lay out the slide; AI is never called for pictures.
window.PhotoCarousel = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hot = line => esc(line).replace(/\*([^*]+)\*/g,'<b>$1</b>');
  const setURL = (jobId,uid) => `/api/production/jobs/${encodeURIComponent(jobId)}/photo-set?timeline=${encodeURIComponent(uid)}`;
  const base = id => `/api/photo-sets/${encodeURIComponent(id)}`;
  const frameNo = t => Math.round(Number(t) * 5) + 1;
  const thumb = (set,t,full = false) => `${base(set.id)}/frame/${frameNo(t)}.jpg${full ? '?full=1' : ''}`;
  const rendered = (set,n,width,style = set.work?.style) => `${base(set.id)}/render/${set.work?.key}/${style}/${n}.jpg?w=${width}&v=${encodeURIComponent(set.updated_at || '')}`;
  const STATUS = {done:'已定稿',doing:'正在做',todo:'排队'};
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
    const ui = {set:null,frames:null,selected:1,onlySharp:true,timer:null,busy:false,textTimer:null,pick:null,workKey:null};
    const close = () => {clearTimeout(ui.timer);clearTimeout(ui.textTimer);dialog.close();dialog.remove();onChange?.(ui.set);};
    dialog.querySelector('[data-close]').onclick = close;
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    const fail = problem => {
      const box = body.querySelector('[data-photo-error]');
      if (box) {box.textContent = problem?.message || '操作没完成，请重试。';box.hidden = false;}
      else if (typeof toast === 'function') toast(problem?.message || '操作没完成',true);
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
      draw();
      clearTimeout(ui.timer);
      if (set.resolve_import?.status === 'running' || set.frames_job?.status === 'running' || set.ai?.status === 'running') ui.timer = setTimeout(load,2000);
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
        <header><strong>${esc(version.key)} · ${esc(version.name_zh)}</strong>${version.recommended ? '<span class="photo-chip">推荐</span>' : ''}${queued[version.key] ? `<span class="photo-chip is-${esc(queued[version.key].status)}">${STATUS[queued[version.key].status]}</span>` : `<label class="photo-pick"><input type="checkbox" data-photo-pick="${esc(version.key)}" ${ui.pick.has(version.key) ? 'checked' : ''}>做这版</label>`}</header>
        <p class="photo-meta">卖给：${esc(version.buyer_zh)}</p>${version.motive_zh ? `<p class="photo-meta">${esc(version.motive_zh)}</p>` : ''}
        <ol>${version.slides.map((slide,index) => `<li><img loading="lazy" src="${thumb(set,slide.frame_time)}" alt=""><div><p class="photo-title">${slide.title_lines.map(hot).join(' / ')}</p><p class="photo-fact">${esc((slide.body_lines || []).map(line => line.replaceAll('*','')).filter(line => line !== (version.cta || '').replaceAll('*','')).join(' / '))}</p><p class="photo-zh">${esc(slide.title_zh)}｜${esc(slide.body_zh)}</p><small>${esc(slide.frame_time)} 秒 · ${esc(slide.picture_zh)}</small></div></li>`).join('')}</ol>
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
      const strip = w.slides.map((slide,index) => `<button type="button" class="photo-slide ${ui.selected === index + 1 ? 'is-selected' : ''}" data-photo-select="${index + 1}" aria-label="第 ${index + 1} 张"><img src="${rendered(set,index + 1,270)}" alt=""></button>`).join('');
      return `<section class="photo-step"><h3><i>3</i>排版<span class="photo-segment">${styles}</span><button type="button" class="subtle compact" data-photo-undo ${w.can_undo ? '' : 'disabled'}>撤销上一步</button></h3><div class="photo-strip">${strip}</div></section>`;
    };
    const editSection = set => {
      const w = set.work;
      if (!w?.slides?.length) return '';
      const n = ui.selected, slide = w.slides[n - 1];
      const image = slide.image || {};
      const frames = (ui.frames?.items || []);
      const sharpCut = (() => {const values = frames.map(item => item.sharp).sort((a,b) => a - b);return values[Math.floor(values.length * .4)] || 0;})();
      const shown = ui.onlySharp ? frames.filter(item => item.sharp >= sharpCut || (image.kind === 'frame' && Math.abs(item.t - image.t) < 1e-6)) : frames;
      const strip = shown.map(item => `<button type="button" class="photo-frame ${image.kind === 'frame' && Math.abs(item.t - image.t) < 1e-6 ? 'is-current' : ''}" data-photo-frame="${item.t}" title="${item.t} 秒"><img loading="lazy" src="${base(set.id)}/frame/${item.n}.jpg" alt=""><span>${item.t.toFixed(1)}</span></button>`).join('');
      const positions = Object.entries(set.positions || {}).map(([key,label]) => `<button type="button" class="${(slide.text_position || 'split') === key ? 'is-current' : ''}" data-photo-position="${esc(key)}">${esc(label)}</button>`).join('');
      const sizes = Object.entries(set.sizes || {}).map(([key,label]) => `<button type="button" class="${(slide.text_size || 'l') === key ? 'is-current' : ''}" data-photo-size="${esc(key)}">${esc(label)}</button>`).join('');
      const source = image.kind === 'upload' ? `上传图：${esc(image.name)}` : `视频 ${esc(image.t)} 秒`;
      const last = n === w.slides.length;
      return `<section class="photo-step photo-editor"><h3><i>4</i>第 ${n} 张 <small>${source}</small></h3>
        <div class="photo-editor-grid"><div class="photo-crop" data-photo-crop tabindex="0" role="slider" aria-label="拖动调整裁切位置" aria-valuetext="上下 ${Math.round((slide.crop_y ?? .5) * 100)}%，左右 ${Math.round((slide.crop_x ?? .5) * 100)}%" title="按住图片上下左右拖动，调整裁出来的部分；方向键微调">
          <img class="photo-preview" src="${rendered(set,n,540)}" alt="第 ${n} 张预览" draggable="false">
          <img class="photo-crop-raw" src="${base(set.id)}/base/${w.key}/${n}?v=${encodeURIComponent(JSON.stringify(slide.image || {}))}" alt="" draggable="false" style="object-position:${(slide.crop_x ?? .5) * 100}% ${(slide.crop_y ?? .5) * 100}%">
          <span class="photo-crop-tip">按住拖动调整裁切</span></div>
        <div class="photo-controls">
          <div class="photo-control"><span>换图</span><label class="photo-check"><input type="checkbox" data-photo-sharp ${ui.onlySharp ? 'checked' : ''}>只看清楚的帧</label><label class="secondary compact photo-upload-button">上传图片<input type="file" accept="image/png,image/jpeg,image/webp" hidden data-photo-image></label><small data-photo-image-progress></small></div>
          <div class="photo-frames" data-photo-frames>${strip || '<span class="context-note">没有抽帧</span>'}</div>
          <div class="photo-control"><span>文字位置</span><span class="photo-segment">${positions}</span><span class="photo-segment" aria-label="字号">${sizes}</span></div>
          <p class="photo-hint">字放在桌面、背景这类空处，别挡刀、磨刀架和箱子；空处不够先挪裁切或缩字，再不行就换一帧。</p>
          <label class="photo-control is-text"><span>标题（一行一句，*星号* 包住的词变色）</span><textarea rows="2" data-photo-title>${esc((slide.title_lines || []).join('\n'))}</textarea></label>
          <label class="photo-control is-text"><span>事实行（最多两行）</span><textarea rows="2" data-photo-body-lines>${esc((slide.body_lines || []).join('\n'))}</textarea></label>
          ${last ? `<label class="photo-control is-text"><span>收口（末张）</span><input type="text" maxlength="60" value="${esc(w.cta || '')}" data-photo-cta></label>` : ''}
          <p class="photo-zh">${esc(slide.title_zh)}｜${esc(slide.body_zh)}</p>
        </div></div></section>`;
    };
    const exportSection = set => {
      const w = set.work;
      if (!w?.slides?.length) return '';
      const exports = (w.exports || []).slice().reverse().map(item => `<li><span>${esc(item.name)}</span><button type="button" class="subtle compact" data-photo-folder="${esc(item.name)}">打开文件夹</button></li>`).join('');
      return `<section class="photo-step"><h3><i>5</i>定稿</h3>
        <details class="photo-caption"><summary>发布文案</summary><label class="field"><span>英文</span><textarea rows="3" maxlength="1200" data-photo-caption-en>${esc(w.caption_en)}</textarea></label><label class="field"><span>中文对照</span><textarea rows="2" maxlength="800" data-photo-caption-zh>${esc(w.caption_zh)}</textarea></label></details>
        <div class="photo-step-line"><span class="context-note">把第 ${esc(w.key)} 版按当前排版导出到「图文成品」新文件夹（发布 JPG＋无损 PNG＋copy.json），旧版本不动。</span><button type="button" class="primary compact" data-photo-export>${w.status === 'done' ? '再导出一次' : '定稿导出'}</button>${set.next ? `<button type="button" class="primary compact" data-photo-work="${esc(set.next.key)}">做下一版：${esc(set.next.key)} ${esc(set.next.name_zh)}</button>` : ''}</div>
        ${exports ? `<ul class="photo-exports">${exports}</ul>` : ''}</section>`;
    };

    function draw() {
      const set = ui.set;
      const scroll = body.scrollTop, frameScroll = body.querySelector('[data-photo-frames]')?.scrollLeft;
      const importing = set.resolve_import?.status === 'running';
      body.innerHTML = `<p class="ai-error" data-photo-error role="alert" hidden></p>${videoSection(set)}${importing ? '' : `${planSection(set)}${queueSection(set)}${styleSection(set)}${editSection(set)}${exportSection(set)}`}`;
      body.scrollTop = scroll;
      const frameBox = body.querySelector('[data-photo-frames]');
      if (frameBox) {
        if (frameScroll != null) frameBox.scrollLeft = frameScroll;
        else {
          const current = frameBox.querySelector('.is-current');
          if (current) frameBox.scrollLeft = current.offsetLeft - frameBox.offsetLeft - (frameBox.clientWidth - current.offsetWidth) / 2;
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
      body.querySelectorAll('[data-photo-select]').forEach(button => button.onclick = () => {ui.selected = Number(button.dataset.photoSelect);draw();});
      const undo = body.querySelector('[data-photo-undo]');
      if (undo) undo.onclick = () => call(`${base(set.id)}/undo`);
      const slideCall = payload => call(`${base(set.id)}/slides/${n}`,payload);
      body.querySelectorAll('[data-photo-frame]').forEach(button => button.onclick = () => slideCall({image:{kind:'frame',t:Number(button.dataset.photoFrame)}}));
      const sharp = body.querySelector('[data-photo-sharp]');
      if (sharp) sharp.onchange = () => {ui.onlySharp = sharp.checked;draw();};
      const image = body.querySelector('[data-photo-image]');
      if (image) image.onchange = async () => {
        const file = image.files[0];if (!file) return;
        const note = body.querySelector('[data-photo-image-progress]');
        try {const staged = await upload(file,'screenshot',value => {if (note) note.textContent = value == null ? '上传中…' : `上传 ${value}%`;});
          await slideCall({image:{kind:'upload',path:staged.path,name:file.name}});}
        catch (problem) {fail(problem);}
      };
      const crop = body.querySelector('[data-photo-crop]');
      if (crop) bindCrop(crop,set.work.slides[n - 1],slideCall);
      body.querySelectorAll('[data-photo-position]').forEach(button => button.onclick = () => slideCall({text_position:button.dataset.photoPosition}));
      body.querySelectorAll('[data-photo-size]').forEach(button => button.onclick = () => slideCall({text_size:button.dataset.photoSize}));
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

    // Drag the picture inside the 3:4 window. While dragging the raw photo is shown (same cover/position rule as
    // the renderer), the text-laid preview comes back after the drop is saved.
    function bindCrop(box,slide,save) {
      const raw = box.querySelector('.photo-crop-raw');
      let pos = {x:slide.crop_x ?? .5,y:slide.crop_y ?? .5}, drag = null;
      const clamp = value => Math.min(1,Math.max(0,value));
      const paint = () => {raw.style.objectPosition = `${pos.x * 100}% ${pos.y * 100}%`;};
      const overflow = () => {
        const width = box.clientWidth, height = box.clientHeight, iw = raw.naturalWidth || 1, ih = raw.naturalHeight || 1;
        const scale = Math.max(width / iw,height / ih);
        return {x:iw * scale - width,y:ih * scale - height};
      };
      const commit = () => {
        box.classList.remove('is-dragging');
        const changed = Math.abs(pos.x - (slide.crop_x ?? .5)) > .002 || Math.abs(pos.y - (slide.crop_y ?? .5)) > .002;
        if (changed) save({crop_y:Math.round(pos.y * 1000) / 1000,crop_x:Math.round(pos.x * 1000) / 1000});
      };
      box.onpointerdown = event => {
        if (event.button !== 0) return;
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
