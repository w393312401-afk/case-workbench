'use strict';

// 挂车图文 for one timeline: upload the no-subtitle export → AI writes 2 per-slide versions (A 达人自述 and B 整套配置) → pick one or several,
// made one after another (each version is its own work with its own pictures, style and exports) →
// pick a type style → swap any slide's picture (another frame or an uploaded photo), drag the crop, text position →
// freeze into 图文成品. The drawer shows one step at a time (视频 → 选版 → 制作 → 定稿) under a step bar like the
// production order's, landing on the step the set is at. A slide can be a collage (左右 / 上下 / 四格): click a cell in the preview, and the frame strip,
// upload and crop drag act on that cell. Slides can be copied, deleted and reordered (drag a thumbnail or use the
// move buttons). Swaps only re-lay out the slide; AI is never called for pictures. Under the words, the user can
// write the slide's meaning in Chinese and ask for 3 English wordings in an American creator voice; nothing changes
// until one is picked (pictures always stay), and a pick can be switched or undone.
// At 选版 the AI suggests search words; they search every synced TikTok comment and the user picks one for the cover
// (a sticker like TikTok's reply sticker, with the commenter's name and picture) and at most one for its own slide, second to last.
window.PhotoCarousel = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hot = line => esc(line).replace(/\*([^*]+)\*/g,'<b>$1</b>');
  // Which model a running AI step is on; older records carry none and show nothing.
  // 图文语言（2026-11-23）：默认跟视频制作单的市场，出逐张表前可以换；每版记着自己的语言，改单张、导出都沿用。
  const marketLabel = (set,id) => (set.markets || []).find(item => item.id === id)?.label || id;
  const marketPick = (set,chosen) => (set.markets || []).length > 1 ? `<select data-photo-market aria-label="图文语言" title="图上文字和发布文案用的语言">${set.markets.map(item => `<option value="${esc(item.id)}" ${item.id === chosen ? 'selected' : ''}>${esc(item.label)}</option>`).join('')}</select>` : '';
  const modelNote = task => task && 'model' in task ? ` · 模型：${esc(window.ModelPick?.describe(task.model,task.reasoning_effort) || task.model || '本机默认模型')}` : '';
  const setURL = (jobId,uid) => `/api/production/jobs/${encodeURIComponent(jobId)}/photo-set?timeline=${encodeURIComponent(uid)}`;
  const base = id => `/api/photo-sets/${encodeURIComponent(id)}`;
  const frameNo = t => Math.round(Number(t) * 5) + 1;
  const thumb = (set,t,full = false) => `${base(set.id)}/frame/${frameNo(t)}.jpg${full ? '?full=1' : ''}`;
  // A slide's picture depends only on its own inputs (and the page count, and the closing line on the last one), so its
  // address does too: after an edit the page asks again only for the slides that changed, the rest come from the browser.
  const tagOf = text => {let h = 2166136261;for (let i = 0;i < text.length;i++) h = Math.imul(h ^ text.charCodeAt(i),16777619);return (h >>> 0).toString(36);};
  const slideTag = (set,n,style) => {
    const slides = set.work?.slides || [];
    return tagOf(JSON.stringify([slides[n - 1] || {},style,slides.length,n === slides.length ? set.work?.cta || '' : '']));
  };
  const rendered = (set,n,width,style = set.work?.style) => `${base(set.id)}/render/${set.work?.key}/${style}/${n}.jpg?w=${width}&v=${slideTag(set,n,style)}`;
  const MAX_ZOOM = 3;
  const zoomOf = value => Math.min(MAX_ZOOM,Math.max(1,Number(value) || 1));
  // Same placement as carousel_render.crop_fit: cover the window, times zoom, then slide the window by crop_x / crop_y.
  const placeRaw = image => {
    const [x,y,zoom] = String(image.dataset.pos || '').split(',').map(Number);
    const box = image.parentElement, width = box?.clientWidth, height = box?.clientHeight, iw = image.naturalWidth, ih = image.naturalHeight;
    if (!iw || !ih || !width || !height) {image.style.objectPosition = `${(x || 0) * 100}% ${(y || 0) * 100}%`;return;}
    const scale = Math.max(width / iw,height / ih) * zoomOf(zoom), sw = iw * scale, sh = ih * scale;
    Object.assign(image.style,{width:`${sw}px`,height:`${sh}px`,left:`${-(sw - width) * x}px`,top:`${-(sh - height) * y}px`,right:'auto',bottom:'auto',maxWidth:'none',objectFit:'fill'});
  };
  const STATUS = {done:'已定稿',doing:'正在做',todo:'排队'};
  // Same cell rectangles as carousel_render.LAYOUTS, as fractions of the 3:4 canvas.
  const CELLS = {single:[[0,0,1,1]],lr:[[0,0,.5,1],[.5,0,1,1]],tb:[[0,0,1,.5],[0,.5,1,1]],grid:[[0,0,.5,.5],[.5,0,1,.5],[0,.5,.5,1],[.5,.5,1,1]]};
  const layoutOf = slide => CELLS[slide?.layout] ? slide.layout : 'single';
  // Every cell of a slide as {image,crop_x,crop_y}; cell 1 is the slide's own picture, missing cells copy it (as the server does).
  const cellsOf = slide => {
    const first = {image:slide.image || {},crop_x:slide.crop_x ?? .5,crop_y:slide.crop_y ?? .5,zoom:zoomOf(slide.zoom)};
    const rest = (slide.cells || []).map(cell => ({image:cell.image || first.image,crop_x:cell.crop_x ?? .5,crop_y:cell.crop_y ?? .5,zoom:zoomOf(cell.zoom)}));
    return CELLS[layoutOf(slide)].map((_,index) => index ? rest[index - 1] || {image:first.image,crop_x:.5,crop_y:.5,zoom:1} : first);
  };
  const tokenHeader = () => ({'X-Workbench-Token': (typeof state === 'object' && state.token) || ''});

  const markup = uid => `<div class="photo-carousel-row" data-photo-carousel="${esc(uid)}"><button type="button" class="secondary compact" data-photo-open>做图文</button><button type="button" class="subtle compact" data-photo-finish hidden title="不再提醒这条图文，做好的内容都保留，可以撤销">完结</button><span data-photo-status class="context-note" role="status"></span><button type="button" class="subtle compact" data-photo-reopen hidden>撤销</button><button type="button" class="danger-outline compact" data-photo-delete hidden>删除</button></div>`;
  const sizeText = bytes => bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1,Math.round(bytes / 1048576))} MB`;
  // Deleting can't be undone, so the first click only reports what would go; the confirm sits on its own line under the row
  // (no dialog on top of the task panel) and a repaint simply takes it away.
  async function askDelete({host,api,jobId,uid,onDone}) {
    const open = host.nextElementSibling?.matches('.photo-delete-confirm') ? host.nextElementSibling : null;
    if (open) {open.remove();return;}
    const url = `/api/production/jobs/${encodeURIComponent(jobId)}/photo-delete`;
    const problem = error => {if (typeof toast === 'function') toast(error?.message || '操作没完成，请重试。',true);};
    let info;
    try {info = await api(url,{method:'POST',body:{timeline:uid,preview:true}});} catch (error) {problem(error);return;}
    const parts = [info.versions ? `${info.versions} 版图文` : '',info.exports ? `${info.exports} 个成品文件夹` : '',info.files ? `${info.files} 个文件，共 ${sizeText(info.bytes)}` : ''].filter(Boolean);
    const box = document.createElement('div');
    box.className = 'photo-delete-confirm';box.setAttribute('role','alert');
    box.innerHTML = `<span>永久删除这条图文的全部本地数据${parts.length ? `：${esc(parts.join('、'))}` : ''}，不能恢复。${info.notion_pages ? `Notion 上的 ${esc(info.notion_pages)} 个页面不会删。` : ''}</span><button type="button" class="danger compact" data-photo-delete-confirm>确认删除</button><button type="button" class="subtle compact" data-photo-delete-cancel>取消</button>`;
    host.after(box);
    box.querySelector('[data-photo-delete-cancel]').onclick = () => box.remove();
    const confirm = box.querySelector('[data-photo-delete-confirm]');
    confirm.onclick = async () => {
      confirm.disabled = true;
      try {
        const result = await api(url,{method:'POST',body:{timeline:uid}});
        box.remove();
        window.TaskActivity?.refreshWork?.();
        if (typeof toast === 'function') toast(result.warnings?.[0] || '图文和本地文件已永久删除。',!!result.warnings?.length);
        await onDone?.();
      } catch (error) {problem(error);confirm.disabled = false;}
    };
  }
  const monthDay = value => {const date = new Date(value);return Number.isNaN(date.getTime()) ? '' : `${String(date.getMonth() + 1).padStart(2,'0')}/${String(date.getDate()).padStart(2,'0')}`;};
  const sentVersions = set => Object.entries(set?.notion || {}).filter(([,status]) => status === 'completed').map(([key]) => key).sort();
  // Every queued version finalized and one of them on Notion is the end of the road without anyone pressing 完结.
  const autoEnded = set => !!(set?.works?.length && set.works.every(work => work.status === 'done') && sentVersions(set).length);
  const decide = (api,jobId,uid,decision) => api(`/api/production/jobs/${encodeURIComponent(jobId)}/photo-decision`,{method:'POST',body:{timeline:uid,decision}}).then(result => {window.TaskActivity?.refreshWork?.();return result;});

  function statusText(set) {
    if (set?.resolve_import?.status === 'running') return '正在从达芬奇导入无字版…';
    if (set?.finished_at) return `已完结 · ${monthDay(set.finished_at)} 手动`;
    if (!set?.exists) return '';
    if (set.frames_job?.status === 'running') return '正在抽帧…';
    if (set.ai?.status === 'running') return 'AI 正在写逐张表…';
    const works = set.works || [];
    if (works.length) {
      const done = works.filter(work => work.status === 'done').length;
      const sent = sentVersions(set);
      const notion = sent.length ? ` · 已上传 Notion ${sent.join('、')}` : '';
      return (autoEnded(set) ? '已完结 · ' : '') + (done === works.length ? `${done} 版都已定稿` : `${done}/${works.length} 版已定稿，正在做 ${set.work?.key || ''}`) + notion;
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

  // ---------- drawer ----------
  // The 图文 opens in a drawer from the right over whatever page the user is on; nothing navigates.
  // Opening another one replaces it, and 打开制作单 is the only way from here to the order page.
  let current = null;
  function open({job,api,uid,timelineName,subtitle = '',onChange}) {
    current?.();
    const dialog = document.createElement('dialog');
    dialog.className = 'rev-drawer photo-carousel-dialog';
    dialog.setAttribute('aria-labelledby','photo-drawer-title');
    const orderHash = `#production/${encodeURIComponent(job.id)}?step=cut`;
    const orderURL = window.WorkbenchShell?.url?.(orderHash,job.product) || orderHash;
    const onOrder = typeof location === 'object' && location.hash.split('?')[0].endsWith('/production/' + encodeURIComponent(job.id));
    const script = String(subtitle || job.title || '').split('`')[0].trim();
    dialog.innerHTML = `<div class="rev-drawer-head photo-drawer-head"><div class="photo-drawer-title"><span class="eyebrow">挂车图文</span><h2 id="photo-drawer-title">${esc(timelineName || '图文')}</h2>${script && script !== timelineName ? `<small class="photo-drawer-sub">${esc(script)}</small>` : ''}</div><div class="photo-drawer-actions">${onOrder ? '' : `<a class="subtle compact" href="${esc(orderURL)}" data-photo-order>打开制作单</a>`}<button type="button" class="danger-outline compact" data-photo-delete-all hidden title="永久删除这条图文在本机的全部数据，删前会先列出要删什么">删除图文</button><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div></div><div class="photo-body" data-photo-body><p class="context-note">正在读取…</p></div>`;
    document.body.append(dialog);dialog.showModal();
    window.DrawerResize?.attach?.(dialog,'photo');
    const body = dialog.querySelector('[data-photo-body]');
    const ui = {set:null,frames:null,selected:1,cell:1,zhDraft:{},onlySharp:true,timer:null,busy:false,textTimer:null,pick:null,workKey:null,step:null,auto:null,shown:null};
    // Chinese shown right under the quoted comment, for reading only: the finished picture keeps the commenter's words.
    const commentTranslation = item => item?.text ? window.ReviewZh?.markup({id:item.id,text:item.text,zh:item.zh},'photo-comment-translation') || '' : '';
    const close = () => {
      if (current !== close) return;
      current = null;clearTimeout(ui.timer);clearTimeout(ui.textTimer);ui.notion?.stop();dialog.close();dialog.remove();onChange?.(ui.set);
    };
    current = close;
    dialog.querySelector('[data-close]').onclick = close;
    dialog.querySelector('[data-photo-order]')?.addEventListener('click',close);
    const head = dialog.querySelector('.photo-drawer-head'), deleteAll = dialog.querySelector('[data-photo-delete-all]');
    // Once everything is gone there is nothing left to show, so the drawer closes and the row behind it reads the set again.
    deleteAll.onclick = () => askDelete({host:head,api,jobId:job.id,uid,onDone:() => {ui.set = null;close();}});
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    // A click on the dimmed page beside the drawer closes it; the backdrop's clicks land on the dialog itself.
    dialog.addEventListener('click',event => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close();
    });
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
    // Picture edits (swap, crop, zoom, text place) keep the drawer usable while one saves: a click made meanwhile waits
    // its turn (the latest one wins) instead of being dropped, so clicking through frames never needs a second try.
    const call = async (path,payload = {},{queue = false} = {}) => {
      if (ui.set?.finished_at && !path.endsWith('/work')) return;
      if (ui.busy) {
        if (queue) ui.queued = ui.queued?.[0] === path ? [path,{...ui.queued[1],...payload}] : [path,payload];
        return;
      }
      ui.busy = true;if (!queue) body.classList.add('is-busy');
      try {await apply(await api(path,{method:'POST',body:payload}));}
      catch (problem) {ui.hold = null;fail(problem);}
      finally {
        ui.busy = false;body.classList.remove('is-busy');
        const next = ui.queued;ui.queued = null;
        if (next) await call(next[0],next[1],{queue:true});
      }
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
      if (set.resolve_import?.status === 'running' || set.frames_job?.status === 'running' || set.ai?.status === 'running' || set.slide_ai?.status === 'running' || set.comment_ai?.status === 'running') ui.timer = setTimeout(load,2000);
    };

    // ----- sections -----
    const videoSection = set => {
      const task = set.resolve_import || {}, running = task.status === 'running';
      const button = `<button type="button" class="${set.exists ? 'secondary' : 'primary'} compact" data-photo-resolve ${running || set.ai?.status === 'running' ? 'disabled' : ''} aria-busy="${running}">${running ? '导出并导入中…' : '从达芬奇导入无字版'}</button>`;
      const percent = Number.isFinite(task.percent) ? ` ${Math.max(0,Math.min(100,task.percent))}%` : '';
      const feedback = task.status === 'failed' ? `<p class="ai-error photo-resolve-note" data-photo-resolve-error role="alert">${esc(task.error)}</p>` : task.message ? `<p class="context-note photo-resolve-note" data-photo-resolve-progress role="status">${esc(task.message)}${percent}</p>` : '';
      const drop = `<label class="photo-drop" data-photo-drop><input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm" hidden data-photo-video><span>${set.exists ? '换一个视频：' : '也可以'}把<strong>关掉字幕轨和屏显轨</strong>导出的视频拖到这里，或点这里选文件</span><small data-photo-upload-progress></small></label>`;
      if (running) return `<section class="photo-step"><h3>视频</h3><div class="photo-step-line">${button}</div>${feedback}</section>`;
      if (!set.exists) return `<section class="photo-step"><h3>视频</h3><div class="photo-step-line photo-resolve-actions">${button}</div>${feedback}${drop}</section>`;
      const job = set.frames_job || {};
      const info = job.status === 'running' ? '正在抽帧…' : job.status === 'failed' ? `<span class="ai-error">${esc(job.error)}</span>` : `${esc(set.video?.name)} · ${esc(set.video?.duration)} 秒 · ${esc(job.count)} 帧`;
      return `<section class="photo-step is-compact"><h3>视频</h3><div class="photo-step-line"><span>${info}</span>${button}<details class="photo-replace"><summary>换视频</summary>${drop}</details></div>${feedback}</section>`;
    };
    const planSection = set => {
      if (!set.exists || set.frames_job?.status !== 'done') return '';
      const ai = set.ai || {};
      if (ai.status === 'running') return `<section class="photo-step"><h3>文案</h3><div class="photo-step-line"><span class="context-note">${esc(ai.progress || 'AI 正在写…')}${modelNote(ai)}</span><button type="button" class="secondary compact" data-photo-cancel>停止</button></div></section>`;
      const ask = `<div class="photo-step-line"><input type="text" maxlength="1000" data-photo-note placeholder="补充说明（可选）：今天挂车价、活动、想强调的点…">${marketPick(set,ui.market || set.market || 'us')}${window.ModelPick?.markup('carousel') || ''}<button type="button" class="${set.versions?.length ? 'secondary' : 'primary'} compact" data-photo-plan>${set.versions?.length ? '重新出逐张表' : 'AI 出逐张表'}</button></div>${ai.status === 'failed' || ai.status === 'cancelled' ? `<p class="ai-error">${esc(ai.error)}</p>` : ''}`;
      if (!set.versions?.length) return `<section class="photo-step"><h3>文案</h3>${ask}</section>`;
      const queued = Object.fromEntries((set.works || []).map(work => [work.key,work]));
      if (!ui.pick) ui.pick = new Set(Object.keys(queued).length ? [] : set.versions.filter(version => version.recommended).map(version => version.key));
      const cards = set.versions.map(version => `<article class="photo-version ${queued[version.key] || ui.pick.has(version.key) ? 'is-chosen' : ''}">
        <header><strong>${esc(version.key)} · ${esc(version.name_zh)}</strong>${version.writing && set.writings?.[version.writing] ? `<span class="photo-chip is-writing">${esc(set.writings[version.writing])}</span>` : ''}${version.market && version.market !== 'us' ? `<span class="photo-chip is-market">${esc(marketLabel(set,version.market))}</span>` : ''}${version.recommended ? '<span class="photo-chip">推荐</span>' : ''}${queued[version.key] ? `<span class="photo-chip is-${esc(queued[version.key].status)}">${STATUS[queued[version.key].status]}</span>` : `<label class="photo-pick"><input type="checkbox" data-photo-pick="${esc(version.key)}" ${ui.pick.has(version.key) ? 'checked' : ''}>做这版</label>`}</header>
        <p class="photo-meta">卖给：${esc(version.buyer_zh)}</p>${version.motive_zh ? `<p class="photo-meta">${esc(version.motive_zh)}</p>` : ''}
        <ol>${version.slides.map((slide,index) => `<li><img loading="lazy" src="${thumb(set,slide.frame_time)}" alt=""><div><p class="photo-title">${slide.title_lines.map(hot).join(' / ')}</p><p class="photo-fact">${esc((slide.body_lines || []).map(line => line.replaceAll('*','')).filter(line => line !== (version.cta || '').replaceAll('*','')).join(' / '))}</p><p class="photo-zh">${esc(slide.title_zh)}｜${esc(slide.body_zh)}</p><small>${esc(slide.frame_time)} 秒${layoutOf(slide) !== 'single' ? ` · 拼图${esc(set.layouts?.[slide.layout] || '')}` : ''} · ${esc(slide.picture_zh)}</small></div></li>`).join('')}</ol>
        ${version.cta ? `<p class="photo-fact">收口：${hot(version.cta)}</p>` : ''}
      </article>`).join('');
      const gaps = set.gaps?.length ? `<details class="photo-gaps"><summary>缺口与没写的说法（${set.gaps.length}）</summary><ul>${set.gaps.map(gap => `<li>${esc(gap)}</li>`).join('')}</ul></details>` : '';
      const picked = set.versions.filter(version => ui.pick.has(version.key) && !queued[version.key]).map(version => version.key);
      const add = picked.length ? `<div class="photo-step-line photo-pick-bar"><span>${(set.works || []).length ? '加进制作队列，排在后面' : '按顺序做'}：${picked.join(' → ')}</span><button type="button" class="primary compact" data-photo-choose>${(set.works || []).length ? `再做这 ${picked.length} 版` : `开始做这 ${picked.length} 版`}</button></div>` : '';
      return `<section class="photo-step"><h3>文案<small>可以选一版或几版，按顺序一版一版做</small></h3><div class="photo-versions">${cards}</div>${set.comments_archived ? '' : commentsSection(set)}${add}${gaps}<details class="photo-replan"><summary>重新出逐张表</summary>${ask}</details></section>`;
    };
    const COMMENT_KIND = {concern:'顾虑',use:'使用反馈'};
    const COMMENT_SLOTS = {cover:['首图','能让人停下来的质疑、提问、处境，贴在首图上，选 1 条'],middle:['中间（可选）','真实使用反馈，单独一张放倒数第二张，标题你在制作时写']};
    const commentsSection = set => {
      const task = set.comment_ai || {};
      const head = `<h4>用户评论<small>AI 给关键词，你可以增删；关键词搜的是全部已同步的 TikTok 评论，评论你来挑。照 TikTok 评论贴纸的样子，带用户名和头像，原文照引</small></h4>`;
      const again = label => `<button type="button" class="secondary compact" data-photo-comments>${label}</button>`;
      if (task.status === 'running') return `<div class="photo-comments">${head}<div class="photo-step-line"><span class="context-note" role="status">${esc(task.progress || 'AI 正在想关键词…')}${modelNote(task)}</span><button type="button" class="secondary compact" data-photo-comments-stop>停止</button></div></div>`;
      if (task.status === 'empty') return `<div class="photo-comments">${head}<div class="photo-step-line"><span class="context-note">${esc(task.note)}</span>${again('再找一次')}</div></div>`;
      const picks = task.picks || [], chosen = new Set(task.chosen || []);
      const meta = item => `${item.username ? `${esc(item.username)} · ` : ''}${item.same_video ? '本条视频' : '同款其他视频'} · ${esc(item.likes)} 赞 · ${esc(String(item.text || '').split(/\s+/).length)} 词`;
      const group = slot => {
        const [title,hint] = COMMENT_SLOTS[slot];
        const mine = picks.find(pick => pick.slot === slot);
        const words = (task.keywords || {})[slot] || [];
        const chips = words.map(k => `<span class="photo-keyword">${esc(k.word)}${k.zh ? `<small>${esc(k.zh)}</small>` : ''}<button type="button" data-photo-keyword-del="${slot}" data-word="${esc(k.word)}" aria-label="去掉 ${esc(k.word)}">×</button></span>`).join('');
        const matches = (task.matches || {})[slot] || [], found = (task.found || {})[slot] || 0;
        const cards = matches.map(item => {
          const on = mine?.id === item.id, elsewhere = !on && chosen.has(item.id);
          return `<div class="photo-comment ${on ? 'is-chosen' : ''}">
            <span class="photo-comment-head">${item.kind ? `<span class="photo-chip ${item.kind === 'concern' ? 'is-writing' : ''}">${esc(COMMENT_KIND[item.kind])}</span>` : ''}<small>${meta(item)}</small></span>
            <q class="photo-comment-quote" lang="en">${esc(item.text)}</q>${commentTranslation(item)}
            <span class="photo-step-line"><small class="photo-comment-meta">命中：${(item.matched || []).map(esc).join('、')}</small>${on ? '<span class="photo-chip">已选</span>' : `<button type="button" class="subtle compact" data-photo-comment-pick="${esc(item.id)}" data-slot="${slot}" ${elsewhere ? 'disabled title="已放在另一个位置"' : ''}>放${slot === 'cover' ? '首图' : '中间'}</button>`}</span></div>`;
        }).join('');
        const result = !words.length ? '<p class="context-note">还没有关键词，自己加一个就会搜。</p>'
          : !found ? '<p class="context-note">这些关键词没搜到评论，换几个词试试。</p>'
          : `<small class="photo-comment-meta">搜到 ${esc(found)} 条${found > matches.length ? `，显示前 ${matches.length} 条` : ''}（命中词多的、本条视频的、有头像的、赞多的在前）</small><div class="photo-comment-list">${cards}</div>`;
        const picked = mine ? `<div class="photo-comment-picked"><span>已选：</span><q lang="en">${esc(mine.text)}</q>${commentTranslation(mine)}<small>${meta(mine)}</small><button type="button" class="subtle compact" data-photo-comment-clear="${slot}">不放了</button></div>` : '';
        return `<div class="photo-comment-group" data-photo-comment-group="${slot}"><h5>${title}<small>${hint}</small></h5>${picked}
          <div class="photo-keywords">${chips}<input type="text" data-photo-keyword-add="${slot}" placeholder="加关键词，回车" aria-label="给${title}加关键词"></div>${result}</div>`;
      };
      const error = ['failed','cancelled'].includes(task.status) ? `<p class="ai-error">${esc(task.error || 'AI 没有完成。')}</p>` : '';
      const open = (set.works || []).filter(work => work.status !== 'done');
      const apply = picks.length && open.length ? `<div class="photo-step-line photo-comment-apply"><span class="context-note">已在做的版本不会自动加：</span>${open.map(work => `<button type="button" class="subtle compact" data-photo-comments-apply="${esc(work.key)}">放进 ${esc(work.key)} 版</button>`).join('')}</div>` : '';
      const pool = task.pool ? `从 ${esc(task.pool)} 条能用的评论里搜${task.own ? `（${esc(task.own)} 条来自本条视频）` : '（本条视频还没有同步到评论，用的是同款其他视频的）'}` : '';
      return `<div class="photo-comments">${head}${error}${group('cover')}${group('middle')}${apply}<div class="photo-step-line photo-comment-foot"><small class="context-note">${pool}</small>${again(task.keywords ? 'AI 重新给关键词' : 'AI 给关键词')}</div></div>`;
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
      return `<section class="photo-step"><h3>排版<span class="photo-segment">${styles}</span><span class="photo-history"><button type="button" class="subtle compact" data-photo-undo ${w.can_undo ? `title="撤销：${esc(w.undo_label)}"` : 'disabled'}>撤销上一步</button><button type="button" class="subtle compact" data-photo-redo ${w.can_redo ? `title="恢复：${esc(w.redo_label)}"` : 'disabled'}>恢复上一步</button></span></h3><div class="photo-strip" data-photo-strip>${strip}</div>${tools}</section>`;
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
      // The AI's own pick and its 1–2 backups that also prove the words (cell 1 of a collage); one click swaps.
      const backups = ui.cell === 1 ? (slide.alternates || []) : [];
      const backupRow = backups.length > 1 ? `<div class="photo-alternates" role="group" aria-label="AI 给的备选画面"><span>AI 备选</span>${backups.map(t => `<button type="button" class="photo-frame ${image.kind === 'frame' && Math.abs(t - image.t) < 1e-6 ? 'is-current' : ''}" data-photo-frame="${t}" title="${t} 秒"><img loading="lazy" src="${thumb(set,t)}" alt=""><span>${Number(t).toFixed(1)}</span></button>`).join('')}</div>` : '';
      const strip = shown.map(item => `<button type="button" class="photo-frame ${image.kind === 'frame' && Math.abs(item.t - image.t) < 1e-6 ? 'is-current' : ''}" data-photo-frame="${item.t}" title="${item.t} 秒"><img loading="lazy" src="${base(set.id)}/frame/${item.n}.jpg" alt=""><span>${item.t.toFixed(1)}</span></button>`).join('');
      const positions = Object.entries(set.positions || {}).map(([key,label]) => `<button type="button" class="${(slide.text_position || 'split') === key ? 'is-current' : ''}" data-photo-position="${esc(key)}">${esc(label)}</button>`).join('');
      const sizes = Object.entries(set.sizes || {}).map(([key,label]) => `<button type="button" class="${(slide.text_size || 'l') === key ? 'is-current' : ''}" data-photo-size="${esc(key)}">${esc(label)}</button>`).join('');
      const layouts = Object.entries(set.layouts || {}).map(([key,label]) => `<button type="button" class="${layout === key ? 'is-current' : ''}" data-photo-layout="${esc(key)}" aria-pressed="${layout === key}">${esc(label)}</button>`).join('');
      const describe = picture => picture.kind === 'upload' ? `上传图：${esc(picture.name)}` : `视频 ${esc(picture.t)} 秒`;
      const source = multi ? cells.map((cell,index) => `${index + 1}. ${describe(cell.image || {})}`).join('　') : describe(image);
      const cellBoxes = CELLS[layout].map(([x0,y0,x1,y1],index) => `<div class="photo-cell ${multi && index + 1 === ui.cell ? 'is-active' : ''}" data-photo-cell="${index + 1}" data-rect="${x0},${y0},${x1},${y1}">
            <img class="photo-crop-raw" src="${cells[index].image?.kind === 'frame' ? thumb(set,cells[index].image.t,true) : `${base(set.id)}/base/${w.key}/${n}?cell=${index + 1}&v=${encodeURIComponent(JSON.stringify(cells[index].image || {}))}`}" alt="" draggable="false" data-pos="${cells[index].crop_x},${cells[index].crop_y},${cells[index].zoom}">
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
            ? `<div class="photo-step-line"><span class="context-note" role="status">${mine ? esc(task.progress || `AI 正在给第 ${n} 张出 3 种说法…`) : `AI 正在给第 ${esc(task.number)} 张出说法…`}${modelNote(task)}</span><button type="button" class="secondary compact" data-photo-rewrite-stop>停止</button></div>`
            : `<div class="photo-step-line"><input type="text" maxlength="500" data-photo-rewrite-note placeholder="其他要求（可选）：短一点、讲质保…" value="${mine ? esc(task.note || '') : ''}">${window.ModelPick?.markup('carousel') || ''}<button type="button" class="secondary compact" data-photo-rewrite>${options ? '再出 3 种' : '出 3 种说法'}</button></div>`}
          ${options ? `<div class="photo-options" role="group" aria-label="AI 给的 3 种说法">${options}</div><p class="photo-hint">点一种换上；不满意可以换另一种，或点「撤销上一步」。</p>` : ''}
          ${mine && (task.status === 'failed' || task.status === 'cancelled') ? `<p class="ai-error" data-photo-rewrite-error>${esc(task.error)}</p>` : ''}</div>`;
      const locked = rewriting && mine ? 'disabled title="AI 正在给这一张出说法"' : '';
      const zoom = cells[ui.cell - 1].zoom;
      const zoomRow = `<div class="photo-zoom" data-photo-zoom-row><span>${multi ? `放大第 ${ui.cell} 格` : '放大'}</span><input type="range" min="1" max="${MAX_ZOOM}" step="0.05" value="${zoom}" data-photo-zoom aria-label="${multi ? `第 ${ui.cell} 格` : '画面'}放大倍数"><output data-photo-zoom-value>${zoom.toFixed(1)}×</output><button type="button" class="subtle compact" data-photo-zoom-reset ${zoom > 1 ? '' : 'disabled'}>还原</button></div>`;
      return `<section class="photo-step photo-editor"><h3>第 ${n} 张 <small>${source}${zoom > 1 ? ` · 放大 ${zoom.toFixed(1)}×` : ''}</small></h3>
        <div class="photo-editor-grid"><div class="photo-crop-col"><div class="photo-crop ${multi ? 'is-collage' : ''}" data-photo-crop tabindex="0" role="slider" aria-label="${multi ? `第 ${ui.cell} 格：拖动调整裁切位置，滚轮放大缩小` : '拖动调整裁切位置，滚轮放大缩小'}" aria-valuetext="上下 ${Math.round(cells[ui.cell - 1].crop_y * 100)}%，左右 ${Math.round(cells[ui.cell - 1].crop_x * 100)}%，放大 ${zoom.toFixed(1)} 倍" title="${multi ? '点一格选中它，再按住拖动调整这一格的裁切；滚轮或 +/- 放大缩小；方向键微调' : '按住图片上下左右拖动，调整裁出来的部分；滚轮或 +/- 放大缩小；方向键微调'}">
          <img class="photo-preview" src="${rendered(set,n,540)}" alt="第 ${n} 张预览" draggable="false">
          ${cellBoxes}
          <span class="photo-crop-tip">${multi ? `点格子选中，拖动调第 ${ui.cell} 格，滚轮放大` : '按住拖动调整裁切，滚轮放大'}</span><span class="photo-crop-busy" aria-hidden="true">更新中…</span></div>${zoomRow}</div>
        <div class="photo-controls">
          <div class="photo-control"><span>版式</span><span class="photo-segment" aria-label="版式">${layouts}</span></div>
          <div class="photo-control"><span>${multi ? `换第 ${ui.cell} 格` : '换图'}</span><label class="photo-check"><input type="checkbox" data-photo-sharp ${ui.onlySharp ? 'checked' : ''}>只看清楚的帧</label><label class="secondary compact photo-upload-button">上传图片<input type="file" accept="image/png,image/jpeg,image/webp" hidden data-photo-image></label><small data-photo-image-progress></small></div>
          ${backupRow}
          <div class="photo-frames" data-photo-frames role="group" aria-label="可选的帧">${strip || '<span class="context-note">没有抽帧</span>'}</div>
          <div class="photo-control"><span>文字位置</span><span class="photo-segment">${positions}</span><span class="photo-segment" aria-label="字号">${sizes}</span></div>
          <p class="photo-hint">${multi ? '拼图只用在前后对比或套装；每格对上它那一半文案，字放在某一格的空处，上下拼图可以放中间压在接缝上。' : '字放在桌面、背景这类空处，别挡刀、磨刀架和箱子；空处不够先挪裁切或缩字，再不行就换一帧。'}</p>
          ${slide.comment ? `<div class="photo-control is-text photo-comment-quoted"><span>${slide.comment.cover ? '首图上的评论贴纸' : '引用的用户评论'}${slide.comment.username ? `（${esc(slide.comment.username)}）` : ''}：原文照引${slide.comment.cover ? '' : '，不想要就删掉这一张'}</span><q lang="en">${esc(slide.comment.text)}</q>${commentTranslation({...slide.comment,id:slide.comment.review_id})}${slide.comment.cover ? '<button type="button" class="subtle compact" data-photo-comment-off>去掉首图贴纸</button>' : ''}</div>` : ''}
          <label class="photo-control is-text"><span>${slide.comment && !slide.comment.cover ? '回答（标题，一行一句，*星号* 包住的词变色）' : '标题（一行一句，*星号* 包住的词变色）'}</span><textarea rows="2" data-photo-title ${locked}>${esc((slide.title_lines || []).join('\n'))}</textarea></label>
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
      return `<section class="photo-step"><h3>定稿</h3>
        ${publishCopy(w)}
        <div class="photo-step-line"><span class="context-note">把第 ${esc(w.key)} 版按当前排版导出到「图文成品」新文件夹（发布 JPG＋无损 PNG＋copy.json），旧版本不动。</span><button type="button" class="primary compact" data-photo-export>${w.status === 'done' ? '再导出一次' : '定稿导出'}</button>${w.status === 'done' ? `<button type="button" class="danger-outline compact" data-photo-unfinalize title="这一版回到正在做，删掉它导出的成品文件夹">撤销定稿</button>` : ''}${set.next ? `<button type="button" class="primary compact" data-photo-work="${esc(set.next.key)}">做下一版：${esc(set.next.key)} ${esc(set.next.name_zh)}</button>` : ''}</div>
        ${exports ? `<ul class="photo-exports">${exports}</ul>` : ''}
        ${w.exports?.length && window.NotionUpload?.photoPanel && !set.finished_at ? '<div data-photo-notion></div>' : ''}</section>`;
    };

    // ----- steps -----
    // Four steps, like the order page's step bar: a later step opens once the one before it has given it something.
    const stepsOf = set => {
      const importing = set.resolve_import?.status === 'running';
      const framed = !!set.exists && set.frames_job?.status === 'done' && !importing;
      const works = set.works || [], w = set.work, made = framed && !!w?.slides?.length;
      const done = works.filter(work => work.status === 'done').length, sent = sentVersions(set).length;
      const later = framed ? '先在「选版」里选一版开始做' : '先导入视频';
      return [
        {key:'video',label:'视频',open:true,done:framed,note:importing ? '导入中' : set.frames_job?.status === 'running' ? '抽帧中' : framed ? '已导入' : '待导入'},
        {key:'plan',label:'选版',open:framed,lock:'先导入视频',done:works.length > 0,
          note:set.ai?.status === 'running' ? 'AI 写作中' : works.length ? `已选 ${works.map(work => work.key).join('、')}` : set.versions?.length ? `${set.versions.length} 版待选` : '待出逐张表'},
        {key:'make',label:'制作',open:made,lock:later,done:made && w.status === 'done',note:made ? `${w.key} 版 · ${w.slides.length} 张` : ''},
        {key:'final',label:'定稿',open:made,lock:later,done:!!works.length && done === works.length,
          note:works.length ? `${done}/${works.length} 版已定稿${sent ? ' · 已传 Notion' : ''}` : ''}
      ];
    };
    // Where the set is at: no video → 视频, no version in the making → 选版, the version being made → 制作, finalized → 定稿.
    const autoStep = (set,steps) => !steps[1].open ? 'video' : !steps[2].open ? 'plan' : set.work?.status === 'done' ? 'final' : 'make';
    const stepBar = (steps,key) => `<nav class="photo-steps" role="tablist" aria-label="图文步骤">${steps.map((step,index) => `<button type="button" role="tab" data-photo-tab="${step.key}" aria-selected="${step.key === key}" tabindex="${step.key === key ? 0 : -1}" class="${step.done ? 'done' : ''}" ${step.open ? '' : `disabled title="${esc(step.lock)}"`}><b aria-hidden="true">${index + 1}</b><span>${step.label}</span>${step.note ? `<small>${esc(step.note)}</small>` : ''}</button>`).join('')}</nav>`;
    const nextButton = (key,label) => `<div class="photo-next"><button type="button" class="secondary compact" data-photo-next="${key}">下一步：${label} →</button></div>`;
    const stepBody = (set,steps,key) => {
      if (key === 'plan') return `${planSection(set)}${steps[2].open ? nextButton('make','制作') : ''}`;
      if (key === 'make') return `${queueSection(set)}${styleSection(set)}${editSection(set)}${nextButton('final','定稿')}`;
      if (key === 'final') return `${queueSection(set)}${exportSection(set)}`;
      return `${videoSection(set)}${steps[1].open && !set.finished_at ? nextButton('plan','选版') : ''}`;
    };

    function draw() {
      const set = ui.set;
      const scroll = body.scrollTop, frameScroll = body.querySelector('[data-photo-frames]')?.scrollTop;
      const focused = document.activeElement;
      const oldPreview = body.querySelector('.photo-preview');
      const shownPreview = oldPreview ? {src:oldPreview.getAttribute('src'),slide:oldPreview.dataset.slide} : null;
      const typing = focused && body.contains(focused) && focused.matches('textarea,input[type=text]')
        ? {name:[...focused.attributes].find(attr => attr.name.startsWith('data-photo'))?.name,value:focused.value,start:focused.selectionStart,end:focused.selectionEnd} : null;
      const steps = stepsOf(set), auto = autoStep(set,steps);
      // The step follows the set when it moves on (import done, a version chosen, finalized, next version); otherwise it
      // stays where the user put it, as long as that step is still open.
      // A failed import keeps the user on 视频 where its error is.
      if (auto !== ui.auto) {ui.auto = auto;if (!(ui.step === 'video' && set.resolve_import?.status === 'failed')) ui.step = auto;}
      if (!steps.find(step => step.key === ui.step)?.open) ui.step = auto;
      const moved = ui.shown !== ui.step;ui.shown = ui.step;
      const closed = set.finished_at ? `<div class="photo-finished-strip" role="status"><span>已完结，撤销后才能继续编辑</span><button type="button" class="secondary compact" data-photo-reopen>撤销完结</button></div>` : '';
      deleteAll.hidden = !set.can_delete;
      body.innerHTML = `${stepBar(steps,ui.step)}${closed}<p class="ai-error" data-photo-error role="alert" hidden></p>${stepBody(set,steps,ui.step)}`;
      // The page's security policy drops style="" written into markup, so cell boxes and crop positions are set here.
      body.querySelectorAll('[data-photo-cell][data-rect]').forEach(node => {
        const [x0,y0,x1,y1] = node.dataset.rect.split(',').map(Number);
        Object.assign(node.style,{left:`${x0 * 100}%`,top:`${y0 * 100}%`,width:`${(x1 - x0) * 100}%`,height:`${(y1 - y0) * 100}%`});
      });
      body.querySelectorAll('.photo-crop-raw[data-pos]').forEach(image => {
        placeRaw(image);
        if (!image.complete) image.addEventListener('load',() => placeRaw(image),{once:true});
      });
      // After an edit the slide is re-laid out on the server; until that picture arrives the old one (or, right after a
      // swap, crop or zoom, the raw picture already in place) stays on screen instead of a blank box.
      const preview = body.querySelector('.photo-preview'), cropBox = body.querySelector('[data-photo-crop]');
      if (preview) {
        const slideKey = `${set.id}:${set.work?.key}:${ui.selected}`, next = preview.getAttribute('src');
        preview.dataset.slide = slideKey;
        const hold = ui.hold === slideKey;
        if (hold) cropBox?.classList.add('is-pending');
        if (shownPreview?.slide === slideKey && shownPreview.src !== next) {
          preview.src = shownPreview.src;cropBox?.classList.add('is-rendering');
          const loader = new Image();
          loader.onload = loader.onerror = () => {
            if (!preview.isConnected || preview.dataset.want !== next) return;
            preview.src = next;cropBox?.classList.remove('is-rendering','is-pending');
            if (ui.hold === slideKey) ui.hold = null;
          };
          preview.dataset.want = next;loader.src = next;
        } else if (hold) {
          const done = () => {cropBox?.classList.remove('is-pending');if (ui.hold === slideKey) ui.hold = null;};
          if (preview.complete) done(); else preview.addEventListener('load',done,{once:true});
        }
      }
      body.scrollTop = moved ? 0 : scroll;
      // Focus before restoring the text: the browser then still sees it as typed and saves it when the field is left.
      if (typing?.name) {
        const node = body.querySelector(`[${typing.name}]`);
        if (node && !node.disabled) {node.focus();node.value = typing.value;try {node.setSelectionRange(typing.start,typing.end);} catch {}}
      }
      const frameBox = body.querySelector('[data-photo-frames]');
      if (frameBox) {
        if (frameScroll != null && !moved) frameBox.scrollTop = frameScroll;
        else {
          const current = frameBox.querySelector('.is-current');
          if (current) frameBox.scrollTop = current.offsetTop - frameBox.offsetTop - (frameBox.clientHeight - current.offsetHeight) / 2;
        }
      }
      bind();
      if (set.finished_at) lockEditing();
    }

    // A finished carousel can still be read: flip through slides and finished versions, copy text, open the export folder.
    function lockEditing() {
      body.querySelectorAll('button,input,textarea,select').forEach(node => {
        if (!node.matches('[data-photo-tab],[data-photo-next],[data-photo-reopen],[data-photo-select],.photo-queue-item.is-done [data-photo-work],[data-photo-folder],[data-photo-copy-title],[data-photo-copy-text],[data-photo-sharp]')) node.disabled = true;
      });
      body.querySelectorAll('[draggable="true"]').forEach(node => {node.draggable = false;});
    }

    function bind() {
      const set = ui.set, n = ui.selected;
      const tabs = [...body.querySelectorAll('[data-photo-tab]')];
      const goStep = key => {if (key === ui.step) return;ui.step = key;draw();body.querySelector(`[data-photo-tab="${key}"]`)?.focus();};
      tabs.forEach(tab => {
        tab.onclick = () => goStep(tab.dataset.photoTab);
        tab.onkeydown = event => {
          const open = tabs.filter(node => !node.disabled), at = open.indexOf(tab);
          const next = {ArrowRight:at + 1,ArrowLeft:at - 1,Home:0,End:open.length - 1}[event.key];
          if (next === undefined) return;
          event.preventDefault();goStep(open[(next + open.length) % open.length].dataset.photoTab);
        };
      });
      body.querySelectorAll('[data-photo-next]').forEach(button => button.onclick = () => goStep(button.dataset.photoNext));
      const reopen = body.querySelector('[data-photo-reopen]');
      if (reopen) reopen.onclick = async () => {
        reopen.disabled = true;
        try {await decide(api,job.id,uid,'reopen');await load();} catch (problem) {fail(problem);reopen.disabled = false;}
      };
      const fromResolve = body.querySelector('[data-photo-resolve]');
      if (fromResolve) fromResolve.onclick = () => call(`/api/production/jobs/${encodeURIComponent(job.id)}/photo-set/from-resolve`,{timeline_uid:uid});
      body.querySelectorAll('[data-photo-video]').forEach(input => input.onchange = () => input.files[0] && sendVideo(input.files[0]));
      body.querySelectorAll('[data-photo-drop]').forEach(zone => {
        zone.ondragover = event => {event.preventDefault();zone.classList.add('is-over');};
        zone.ondragleave = () => zone.classList.remove('is-over');
        zone.ondrop = event => {event.preventDefault();zone.classList.remove('is-over');const file = event.dataTransfer.files[0];if (file) sendVideo(file);};
      });
      const plan = body.querySelector('[data-photo-plan]');
      const marketSelect = body.querySelector('[data-photo-market]');
      if (marketSelect) marketSelect.onchange = () => {ui.market = marketSelect.value;};
      if (plan) plan.onclick = async () => {await call(`${base(set.id)}/plan`,{note:body.querySelector('[data-photo-note]')?.value || '',...(marketSelect ? {market:marketSelect.value} : {}),...(window.ModelPick?.value('carousel') || {})});if (ui.set?.ai?.status === 'running') window.ModelPick?.used('carousel');};
      void window.ModelPick?.mount(body);
      const cancel = body.querySelector('[data-photo-cancel]');
      if (cancel) cancel.onclick = () => call(`${base(set.id)}/cancel`);
      body.querySelectorAll('[data-photo-pick]').forEach(box => box.onchange = () => {
        if (box.checked) ui.pick.add(box.dataset.photoPick); else ui.pick.delete(box.dataset.photoPick);
        draw();
      });
      const suggest = body.querySelector('[data-photo-comments]');
      if (suggest) suggest.onclick = async () => {await call(`${base(set.id)}/comments`,{...(window.ModelPick?.value('carousel') || {})});};
      const stopComments = body.querySelector('[data-photo-comments-stop]');
      if (stopComments) stopComments.onclick = () => call(`${base(set.id)}/comments-stop`);
      // Search words: removing or adding one searches again right away (in the library, no AI).
      const searchWith = (slot,change) => {
        const keywords = {};
        for (const key of ['cover','middle']) keywords[key] = ((set.comment_ai?.keywords || {})[key] || []).map(k => k.word);
        keywords[slot] = change(keywords[slot]);
        return call(`${base(set.id)}/comments-search`,{keywords});
      };
      body.querySelectorAll('[data-photo-keyword-del]').forEach(button => button.onclick = () => searchWith(button.dataset.photoKeywordDel,words => words.filter(word => word !== button.dataset.word)));
      body.querySelectorAll('[data-photo-keyword-add]').forEach(input => input.onkeydown = event => {
        if (event.key !== 'Enter' || event.isComposing) return;
        event.preventDefault();
        const added = input.value.split(/[,，、]/).map(word => word.trim()).filter(Boolean);
        if (added.length) searchWith(input.dataset.photoKeywordAdd,words => [...words,...added]);
      });
      // One comment per place: picking another swaps it in.
      body.querySelectorAll('[data-photo-comment-pick]').forEach(button => button.onclick = () => call(`${base(set.id)}/comments-pick`,{slot:button.dataset.slot,id:button.dataset.photoCommentPick}));
      body.querySelectorAll('[data-photo-comment-clear]').forEach(button => button.onclick = () => call(`${base(set.id)}/comments-pick`,{slot:button.dataset.photoCommentClear,id:null}));
      body.querySelectorAll('[data-photo-comments-apply]').forEach(button => button.onclick = () => call(`${base(set.id)}/comments-apply`,{key:button.dataset.photoCommentsApply}));
      const commentOff = body.querySelector('[data-photo-comment-off]');
      if (commentOff) commentOff.onclick = () => call(`${base(set.id)}/slides/${n}`,{comment:null});
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
      const slideCall = payload => call(`${base(set.id)}/slides/${n}`,payload,{queue:true});
      const cellCall = payload => slideCall(ui.cell > 1 ? {...payload,cell:ui.cell} : payload);
      const holdKey = `${set.id}:${set.work?.key}:${n}`;
      body.querySelectorAll('[data-photo-frame]').forEach(button => button.onclick = () => {
        // Show the picked frame right away (the raw picture, without the words); the laid-out slide replaces it when ready.
        const t = Number(button.dataset.photoFrame), crop = body.querySelector('[data-photo-crop]');
        const raw = crop?.querySelector(`[data-photo-cell="${ui.cell}"] .photo-crop-raw`);
        if (raw && !set.finished_at) {
          raw.addEventListener('load',() => placeRaw(raw),{once:true});
          raw.src = thumb(set,t,true);crop.classList.add('is-pending');ui.hold = holdKey;
          body.querySelectorAll('[data-photo-frame].is-current').forEach(node => node.classList.remove('is-current'));
          button.classList.add('is-current');
        }
        cellCall({image:{kind:'frame',t}});
      });
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
      if (crop && !set.finished_at) bindCrop(crop,cellsOf(set.work.slides[n - 1]),payload => {ui.hold = holdKey;return cellCall(payload);});
      body.querySelectorAll('[data-photo-position]').forEach(button => button.onclick = () => slideCall({text_position:button.dataset.photoPosition}));
      body.querySelectorAll('[data-photo-size]').forEach(button => button.onclick = () => slideCall({text_size:button.dataset.photoSize}));
      const rewrite = body.querySelector('[data-photo-rewrite]');
      if (rewrite) rewrite.onclick = () => {
        // Unchanged Chinese means "same meaning, say it better"; changed Chinese is the new meaning to write from.
        const slide = set.work.slides[n - 1], zh = (body.querySelector('[data-photo-zh]')?.value || '').trim();
        const current = [slide.title_zh,slide.body_zh].filter(Boolean).join('\n').trim();
        void call(`${base(set.id)}/slides/${n}/rewrite`,{note:body.querySelector('[data-photo-rewrite-note]')?.value || '',zh:zh === current ? '' : zh,...(window.ModelPick?.value('carousel') || {})}).then(() => {if (ui.set?.slide_ai?.status === 'running') window.ModelPick?.used('carousel');});
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
          const basketAdded = result.basket?.added || 0;
          if (typeof toast === 'function') toast(`已导出到 图文成品/${result.export?.name}${basketAdded ? `；${basketAdded} 条评论贴纸已放进摘录篮` : ''}`);
          if (basketAdded) window.MaterialBasket?.refresh?.(true);}
        catch (problem) {fail(problem);exporter.disabled = false;exporter.textContent = '定稿导出';}
      };
      // 撤销定稿 deletes this version's export folders, so like 删除 the first click only reports what would go.
      const unfinalize = body.querySelector('[data-photo-unfinalize]');
      if (unfinalize) unfinalize.onclick = async () => {
        const line = unfinalize.closest('.photo-step-line');
        const shown = line.nextElementSibling?.matches('.photo-delete-confirm') ? line.nextElementSibling : null;
        if (shown) {shown.remove();return;}
        if (ui.busy) return;
        const url = `${base(set.id)}/unfinalize`, key = set.work.key;
        let info;
        try {info = await api(url,{method:'POST',body:{key,preview:true}});} catch (problem) {fail(problem);return;}
        const files = info.exports ? `它导出的 ${info.exports} 个成品文件夹${info.files ? `（${info.files} 个文件，共 ${sizeText(info.bytes)}）` : ''}会永久删除，不能恢复` : '本机没有它的成品文件夹要删';
        const box = document.createElement('div');
        box.className = 'photo-delete-confirm';box.setAttribute('role','alert');
        box.innerHTML = `<span>撤销第 ${esc(key)} 版定稿：这一版回到「正在做」，文案、排版和换图都保留；${esc(files)}。${info.notion_page ? 'Notion 上的页面不会删，重新定稿后再上传会更新那一页。' : ''}</span><button type="button" class="danger compact" data-photo-unfinalize-confirm>确认撤销</button><button type="button" class="subtle compact" data-photo-unfinalize-cancel>取消</button>`;
        line.after(box);
        box.querySelector('[data-photo-unfinalize-cancel]').onclick = () => box.remove();
        const confirm = box.querySelector('[data-photo-unfinalize-confirm]');
        confirm.onclick = async () => {
          if (ui.busy) return;
          ui.busy = true;confirm.disabled = true;body.classList.add('is-busy');
          try {
            const result = await api(url,{method:'POST',body:{key}});
            ui.busy = false;
            await apply(result);
            if (typeof toast === 'function') toast(result.warnings?.[0] || `第 ${key} 版已撤销定稿，成品文件夹已删除。`,!!result.warnings?.length);
          } catch (problem) {fail(problem);confirm.disabled = false;}
          finally {ui.busy = false;body.classList.remove('is-busy');}
        };
      };
      // One Notion panel per version and export; a new export starts a fresh panel that reads the new pictures.
      const notionHost = body.querySelector('[data-photo-notion]');
      if (notionHost) {
        const key = `${set.id}|${set.work.key}|${(set.work.exports || []).length}`;
        if (ui.notion?.key !== key) {
          ui.notion?.stop();
          ui.notion = Object.assign(window.NotionUpload.photoPanel({api,setId:set.id,work:set.work.key}),{key});
        }
        ui.notion.mount(notionHost);
      }
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
      let pos = {x:cell.crop_x,y:cell.crop_y,z:zoomOf(cell.zoom)}, drag = null;
      const clamp = value => Math.min(1,Math.max(0,value));
      const row = box.parentElement?.querySelector('[data-photo-zoom-row]');
      const slider = row?.querySelector('[data-photo-zoom]'), shown = row?.querySelector('[data-photo-zoom-value]'), reset = row?.querySelector('[data-photo-zoom-reset]');
      const paint = () => {
        raw.dataset.pos = `${pos.x},${pos.y},${pos.z}`;placeRaw(raw);
        if (slider && document.activeElement !== slider) slider.value = String(pos.z);
        if (shown) shown.textContent = `${pos.z.toFixed(1)}×`;
        if (reset) reset.disabled = pos.z <= 1;
      };
      const overflow = () => {
        const width = active.clientWidth, height = active.clientHeight, iw = raw.naturalWidth || 1, ih = raw.naturalHeight || 1;
        const scale = Math.max(width / iw,height / ih) * pos.z;
        return {x:iw * scale - width,y:ih * scale - height};
      };
      const commit = () => {
        clearTimeout(box._cropTimer);
        const moved = Math.abs(pos.x - cell.crop_x) > .002 || Math.abs(pos.y - cell.crop_y) > .002, zoomed = Math.abs(pos.z - zoomOf(cell.zoom)) > .004;
        if (!moved && !zoomed) {box.classList.remove('is-dragging');return;}
        // The raw picture stays up (now as "pending") until the re-laid-out slide has loaded.
        box.classList.add('is-pending');box.classList.remove('is-dragging');
        save({...(moved ? {crop_y:Math.round(pos.y * 1000) / 1000,crop_x:Math.round(pos.x * 1000) / 1000} : {}),...(zoomed ? {zoom:Math.round(pos.z * 100) / 100} : {})});
      };
      const zoomTo = (value,later) => {
        pos.z = Math.round(zoomOf(value) * 100) / 100;
        box.classList.add('is-dragging');paint();
        clearTimeout(box._cropTimer);
        if (later) box._cropTimer = setTimeout(commit,later);
      };
      if (slider) {
        slider.oninput = () => zoomTo(Number(slider.value));
        slider.onchange = () => {zoomTo(Number(slider.value));commit();};
      }
      if (reset) reset.onclick = () => {zoomTo(1);commit();};
      // Over the picture the wheel zooms (it does not scroll the drawer), like a photo editor.
      box.onwheel = event => {
        event.preventDefault();
        zoomTo(pos.z * Math.exp(-Math.max(-120,Math.min(120,event.deltaY)) / 600),500);
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
        if (['+','=','-','_'].includes(event.key)) {
          event.preventDefault();
          zoomTo(pos.z + (['+','='].includes(event.key) ? .1 : -.1),600);
          return;
        }
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
      if (ui.busy || ui.set?.finished_at || ui.set?.resolve_import?.status === 'running') return;
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
    const finish = root.querySelector('[data-photo-finish]'), reopen = root.querySelector('[data-photo-reopen]'), remove = root.querySelector('[data-photo-delete]');
    const timeline = (job.timelines || []).find(row => row.unique_id === uid);
    const show = set => {
      if (!root.isConnected) return;
      status.textContent = statusText(set);
      const ended = !!set?.finished_at || autoEnded(set);
      button.textContent = ended ? '查看图文' : set?.exists ? '继续做图文' : '做图文';
      finish.hidden = ended || !set?.can_finish;
      reopen.hidden = !set?.finished_at;
      remove.hidden = !set?.can_delete;
    };
    const change = (control,decision) => async () => {
      control.disabled = true;
      try {await decide(api,job.id,uid,decision);show(await api(setURL(job.id,uid)));}
      catch (problem) {if (typeof toast === 'function') toast(problem?.message || '操作没完成，请重试。',true);}
      finally {control.disabled = false;}
    };
    finish.onclick = change(finish,'finish');
    reopen.onclick = change(reopen,'reopen');
    remove.onclick = () => askDelete({host:root,api,jobId:job.id,uid,onDone:() => api(setURL(job.id,uid)).then(show).catch(() => {})});
    // The window's copy of the set has no Notion summary, so the line reads the set again once the window closes.
    button.onclick = () => open({job,api,uid,timelineName:timeline?.name,onChange:() => api(setURL(job.id,uid)).then(show).catch(() => {})});
    try {show(await api(setURL(job.id,uid)));} catch {status.textContent = '';}
  }
  // A 图文 task (抽帧, 逐张表, 改写, 挑评论, 导入无字版) names its order and either its timeline or its set:
  // find that timeline and open its drawer. Resolves false when the timeline can no longer be found.
  async function openFor({jobId,source,api,onChange}) {
    const job = (await api(`/api/production/jobs/${encodeURIComponent(jobId)}`))?.job;
    const lines = (job?.timelines || []).filter(line => line?.unique_id);
    let line = lines.find(row => row.unique_id === source);
    if (!line && /^PHS-/.test(source || '')) {
      for (const row of lines) {
        try {if ((await api(setURL(jobId,row.unique_id)))?.id === source) {line = row;break;}} catch {/* that timeline has no set */}
      }
    }
    if (!line) return false;
    open({job,api,uid:line.unique_id,timelineName:line.name,onChange});
    return true;
  }
  return {markup,bind,open,openFor,askDelete,close:() => current?.()};
})();
