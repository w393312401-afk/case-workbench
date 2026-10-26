'use strict';

// A local publication receipt: frozen review version + managed video + actual post.
window.VideoPublication = (() => {
  const requests = new Map();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const localDate = () => {const value = new Date();return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;};
  const size = value => `${(Number(value || 0) / 1024 / 1024).toFixed(1)} MB`;
  const base = id => `/api/production/jobs/${encodeURIComponent(id)}/publication`;
  const storageKey = id => `video-publication:${id}`;
  function remember(id,saved) {
    requests.set(id,saved);
    try {sessionStorage.setItem(storageKey(id),JSON.stringify(saved));} catch {}
  }
  function pending(id,body) {
    const signature = JSON.stringify(body);
    let saved = requests.get(id);
    if (!saved) {try {saved = JSON.parse(sessionStorage.getItem(storageKey(id)) || 'null');} catch {}}
    if (!saved || saved.signature !== signature) saved = {signature,body:{...body,request_id:crypto.randomUUID()}};
    remember(id,saved);
    return saved.body;
  }
  function clearPending(id) {
    requests.delete(id);
    try {sessionStorage.removeItem(storageKey(id));} catch {}
  }
  // Takes read as the version number the order shows; the raw ID stays in the tooltip.
  const takeLabel = (job,take,historical = false) => take?.version ? `配音第 ${take.version} 版` : take?.id ? historical ? '当时选用配音' : '本次选用配音' : '配音版本未保留';
  // 发布日历 is kept outside the workbench; the mark only reminds you which posts still need uploading.
  function calendarLine(item,current) {
    const uploaded = item.calendar_uploaded;
    const state = uploaded === true ? '<span class="script-status published">已上传</span>' : uploaded === false ? '<span class="script-status">未上传</span>' : '<span class="context-note">未记录</span>';
    const toggle = current ? ` <button type="button" class="secondary compact" data-calendar-toggle="${uploaded === true ? '0' : '1'}" data-publication-id="${esc(item.id)}">${uploaded === true ? '改回未上传' : '标记为已上传'}</button>` : '';
    return `<p class="video-publication-calendar">发布日历 ${state}${toggle}</p>`;
  }
  // The mark is set by the calendar line (before posting) or the receipt toggle; the form only carries it along.
  const calendarChoice = uploaded => `<input type="hidden" name="calendar_uploaded" value="${uploaded === true ? '1' : '0'}">`;
  // One script can be registered under several posts; the order is by first registration.
  // The registered link, or the standard one from account and ID (TikTok redirects an unknown handle).
  const tiktokUrl = (record,kind = 'video') => /^https?:\/\//.test(record?.post_url || '') ? record.post_url : record?.post_id ? `https://www.tiktok.com/@${encodeURIComponent(String(record.account || '').replace(/^@/,'') || '_')}/${kind}/${record.post_id}` : '';
  let scriptPosts = [];
  // Rides at the end of the post line, beside the post it counts.
  const registrationTag = (posts,postId) => {
    const number = (posts || []).indexOf(String(postId)) + 1;
    return number && posts.length > 1 ? `<small class="video-publication-aside" data-script-registration title="此脚本第 ${number} 次登记（共 ${posts.length} 次）">第 ${number}/${posts.length} 次登记</small>` : '';
  };
  const registrationHint = posts => posts?.length ? `<p class="context-note" data-script-registration>此脚本已登记 ${posts.length} 次，本次将是第 ${posts.length + 1} 次登记。</p>` : '';
  // Before posting there is no registration to carry the mark, so it is kept on the order itself.
  const calendarPlanMarkup = () => '<p class="video-publication-calendar" data-calendar-plan>发布日历 <span class="context-note">读取中…</span></p>';
  async function bindCalendarPlan(host,contentId,api,{form = null,after} = {}) {
    const line = host.querySelector('[data-calendar-plan]');
    if (!line) return;
    const url = `/api/content/calendar`, save = body => api(url,{method:'POST',body:{id:contentId,...body}});
    const show = state => {
      const day = state.calendar_date || localDate(), uploaded = state.calendar_uploaded === true;
      line.innerHTML = `发布日历 ${uploaded ? '<span class="script-status published">已上传</span>' : '<span class="script-status">未上传</span>'} <label class="video-publication-calendar-date">发布日期 <input type="date" value="${esc(day)}"></label> <button type="button" class="secondary compact">${uploaded ? '改回未上传' : '标记为已上传'}</button>`;
      // The registration below carries the planned day as its posting day.
      const field = form?.elements.published_on;
      if (field && !form._postDate?.picked()) field.value = day;
      const input = line.querySelector('input'), button = line.querySelector('button');
      input.onchange = async () => {
        if (!input.value) {input.value = day;return;}
        input.disabled = true;
        try {show({...state,...await save({calendar_date:input.value}).then(() => ({calendar_date:input.value}))});await after?.();}
        catch (problem) {input.disabled = false;toast(problem.message || '没能保存发布日期。',true);}
      };
      button.onclick = async () => {
        button.disabled = true;
        try {await save({calendar_uploaded:!uploaded,calendar_date:input.value || day});show({calendar_uploaded:!uploaded,calendar_date:input.value || day});await after?.();}
        catch (problem) {button.disabled = false;toast(problem.message || '没能更新发布日历状态。',true);}
      };
    };
    try {show(await api(`${url}?id=${encodeURIComponent(contentId)}`));}
    catch {line.innerHTML = '发布日历 <span class="context-note">暂时读不到状态</span>';}
  }
  function receipt(job,item,current = false) {
    const jobId = job.id, source = item.source || {}, audio = source.take || {}, timeline = source.timeline || {};
    return `<article class="video-publication-receipt"><div class="video-publication-title"><strong>${esc(item.account)} · ${esc(item.published_on)}</strong><span class="script-status published">已登记发布</span></div>
      <p>帖子 <a href="${esc(tiktokUrl(item,'video'))}" target="_blank" rel="noopener noreferrer" title="在 TikTok 打开">${esc(item.post_id)} ↗</a>${typeof copyIdButton === 'function' ? copyIdButton(item.post_id,'视频 ID') : ''} · 脚本 v${esc(item.script_version)}${registrationTag(scriptPosts,item.post_id)}${item.file ? '' : '<small class="video-publication-aside" title="只登记了视频 ID，没有绑定成片文件">未绑定成片</small>'}</p>
      <p><span title="${esc(audio.id)}">${esc(takeLabel(job,audio,true))}</span> · ${esc(timeline.name || timeline.unique_id)}${source.manual_completion?.at ? ' · 人工完成' : source.edit ? ` · <span title="${esc(source.edit.id)}">AI 剪辑</span>` : ' · 完成方式未保留'}</p>
      ${item.file ? `<p>${esc(item.file.name)} · ${size(item.file.size)} <a href="${base(jobId)}/${encodeURIComponent(item.id)}/download">领取登记成片</a></p>` : ''}
      ${current && !item.source_current ? '<p class="video-publication-warning">当前制作版本或审查状态已变化；这条发布记录仍保留当时的成片、配音和审查依据。</p>' : ''}
      ${current && item.file && !item.file_available ? '<p class="video-publication-warning">托管成片缺失或大小有变化，请从完整备份恢复。</p>' : ''}
      ${calendarLine(item,current)}
      ${item.note ? `<p>${esc(item.note)}</p>` : ''}
      ${window.DeliveryProvenance?.render({kind:'video',record:item,owner:job,copyTake:true,
        rows:`<dt>登记时间</dt><dd>${esc(localTime(item.registered_at))}</dd>${item.file ? `<dt>原成片路径</dt><dd>${esc(item.file.original_path)}</dd>` : ''}<dt>审查依据</dt><dd>${esc(localTime((source.manual_completion || source.edit?.approval)?.at) || '未保留')}</dd>`}) || ''}
      ${current && item.file ? '<button type="button" class="secondary" data-verify-video>核对保存的成片</button><p class="context-note" data-verification role="status"></p>' : ''}</article>`;
  }
  // What gets pasted at publish time (Notion, then TikTok): the adopted script's 🔴 title lines, Chinese and English text.
  const copyIcon = () => typeof icon === 'function' ? icon('copy') : '';
  function copyBlock(copy,{collapsed = false} = {}) {
    if (!copy || !(copy.titles?.length || copy.zh || copy.en)) return '';
    const body = `<section class="ai-title-lines video-publish-copy"><div class="ai-title-lines-head"><h3>发布文案 <small>来自已采纳脚本${copy.version ? ` 第 ${esc(copy.version)} 版` : ''} · 点一下复制</small></h3></div>
      ${copy.titles?.length ? `<h4>标题与主题标签</h4>${copy.titles.map(line => `<button type="button" class="ai-title-line" data-copy-title="${esc(line)}" title="复制这一行"><span>${esc(line)}</span>${copyIcon()}</button>`).join('')}` : '<p class="context-note">这条脚本没有写标题与主题标签。</p>'}
      ${copy.zh || copy.en ? `<div class="video-publish-copy-actions"><button type="button" class="secondary compact" data-copy-publish="text">${copyIcon()}复制文案</button></div>` : ''}
      ${copy.zh || copy.en ? `<details><summary>查看文案</summary>${copy.zh ? `<p>${esc(copy.zh)}</p>` : ''}${copy.en ? `<p>${esc(copy.en)}</p>` : ''}</details>` : ''}</section>`;
    return collapsed ? `<div class="video-publish-copy-compact">${copy.zh || copy.en ? `<button type="button" class="subtle compact" data-copy-publish="text">${copyIcon()}复制文案</button>` : ''}<details class="video-publish-copy-fold"><summary>发布文案 · 展开标题与全文</summary>${body}</details></div>` : body;
  }
  function bindCopy(host,copy) {
    const write = (text,message) => typeof copyText === 'function' ? copyText(text,message) : navigator.clipboard.writeText(text);
    host.querySelectorAll('[data-copy-title]').forEach(button => button.addEventListener('click',() => {
      write(button.dataset.copyTitle,'标题与主题标签已复制');button.classList.add('copied');setTimeout(() => button.classList.remove('copied'),1200);
    }));
    host.querySelectorAll('[data-copy-publish]').forEach(button => button.addEventListener('click',() => {
      // One button for the copy pasted at publish time: Chinese, a blank line, then English.
      write([copy.zh,copy.en].filter(Boolean).join('\n\n'),copy.zh && copy.en ? '中英文案已复制' : '文案已复制');
    }));
  }
  // Only the video ID needs typing: a pasted link also gives the account, the account defaults to the last one used,
  // and a video already in the sales data brings its own account and date.
  const lastAccount = () => {try {return localStorage.getItem('video-publication:account') || '';} catch {return '';}};
  const fromLink = value => {
    const match = String(value || '').match(/tiktok\.com\/@([^/?#\s]+)\/(?:video|photo)\/(\d{6,25})/i);
    return match ? {account:decodeURIComponent(match[1]),post_id:match[2],post_url:match[0].toLowerCase().includes('/photo/') ? `https://www.tiktok.com/@${match[1]}/photo/${match[2]}` : `https://www.tiktok.com/@${match[1]}/video/${match[2]}`} : null;
  };
  // A post ID starts with its posting time (top 32 bits, Unix seconds). Shop exports write that time at a fixed
  // UTC−8 all year, so this is the day the sales data will show. The ID is read the way normalize reads it;
  // one that cannot be a past post (short, pre-2015, future, overlong) gives ''.
  function postedOn(value) {
    const id = fromLink(value)?.post_id || String(value ?? '').replace(/\D/g,'');
    if (!/^\d{15,25}$/.test(id)) return '';
    const seconds = Number(BigInt(id) >> 32n);
    if (seconds < 1420070400 || seconds > Date.now() / 1000 + 86400) return '';
    const day = new Date((seconds - 8 * 3600) * 1000).toISOString().slice(0,10);
    return day <= localDate() ? day : '';
  }
  // Typing or pasting the ID fills in its posting day. A date that already differs from the ID when the form
  // opens (a saved registration or draft) counts as set by hand, like one changed by hand, and is left alone.
  function followPostDate(form,name = 'post_id') {
    if (form._postDate) return form._postDate;
    const field = form.elements[name], date = form.elements.published_on, opened = postedOn(field.value);
    let picked = Boolean(opened) && date.value !== opened;
    date.addEventListener('input',() => {picked = true;});
    const sync = () => {
      const day = picked ? '' : postedOn(field.value);
      if (day) date.value = day;
      return day;
    };
    field.addEventListener('input',sync);
    return form._postDate = {sync,picked:() => picked};
  }
  function normalize(values,path = 'video') {
    const linked = fromLink(values.post_id);
    const out = {...values,post_id:linked ? linked.post_id : String(values.post_id || '').replace(/\D/g,'') || String(values.post_id || '').trim()};
    if (linked && !String(out.account || '').trim()) out.account = linked.account;
    out.account = String(out.account || '').trim().replace(/^@/,'');
    if (!String(out.post_url || '').trim()) out.post_url = linked ? linked.post_url : out.account && /^\d{6,25}$/.test(out.post_id) ? `https://www.tiktok.com/@${encodeURIComponent(out.account)}/${path}/${out.post_id}` : '';
    return out;
  }
  async function assistIdentity(form,api) {
    let videos = null;
    const known = async () => {if (!videos) {try {videos = (await api('/api/performance/videos?shop=all')).videos || [];} catch {videos = [];}}return videos;};
    known().then(list => {
      const names = [...new Set([lastAccount(),...list.map(video => video.account)].filter(Boolean))];
      form.querySelector('#video-publication-accounts').innerHTML = names.map(name => `<option value="${esc(name)}"></option>`).join('');
    });
    const field = form.elements.post_id, hint = form.querySelector('[data-account-hint]'), dated = followPostDate(form);
    field.addEventListener('change',async () => {
      const linked = fromLink(field.value);
      if (linked) {field.value = linked.post_id;form.elements.account.value = linked.account;if (!form.elements.post_url.value) form.elements.post_url.value = linked.post_url;}
      const day = dated.sync();
      const found = (await known()).find(video => String(video.video_id) === field.value.trim());
      const notes = [];
      if (found) {
        const filled = [];
        if (found.account) {form.elements.account.value = found.account;filled.push('账号');}
        if (found.posted_on && found.posted_on <= localDate() && !dated.picked()) {form.elements.published_on.value = found.posted_on;filled.push('日期');}
        if (filled.length) notes.push(`已按销售数据里的这条视频补上${filled.join('和')}`);
      } else if (linked) notes.push('已从链接读取账号');
      if (day && !notes.some(note => note.endsWith('日期'))) notes.push('日期按视频 ID 补上');
      hint.textContent = notes.join('，');
    });
  }
  // 转成图文: frames of this finished video → AI 逐张表 + picked frames + composites → a linked 图文制作单.
  const effortNames = {none:'不启用',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高',ultra:'极致'};
  let aiOptions = null;
  async function loadAiOptions(api) {
    if (aiOptions) return aiOptions;
    try {const result = await api('/api/ai/options');aiOptions = {models:Array.isArray(result.models) ? result.models : [],defaults:result.default_settings || {}};}
    catch {aiOptions = {models:[],defaults:{}};}
    return aiOptions;
  }
  const carouselURL = (id,product) => window.WorkbenchShell?.url?.(`#production/${encodeURIComponent(id)}`,product) || `#production/${encodeURIComponent(id)}`;
  const tableURL = run => window.WorkbenchShell?.url?.(`#content/${encodeURIComponent(run.script_id)}?table=${encodeURIComponent(run.id)}`,run.product) || `#content/${encodeURIComponent(run.script_id)}?table=${encodeURIComponent(run.id)}`;
  function runCard(run) {
    const canDownload = run.delivery_id && run.delivery_available === true;
    const waiting = run.status === 'running' && (run.phase === '等待达芬奇' || String(run.progress || '').startsWith('等待达芬奇'));
    if (run.status === 'running') return `<div class="video-carousel-run is-running"><p><strong>${waiting ? '等待达芬奇' : run.autopilot ? 'AI 正在全程制作图文' : '⏳ 正在转图文'}</strong> ${esc(run.progress || '请稍候')}</p><p class="context-note">${esc(run.video_source || '')}${run.frames ? ` · 留下 ${run.frames} 帧不同画面` : ''} · 可以离开这页，后台继续。</p><button type="button" class="secondary compact" data-video-carousel-stop="${esc(run.id)}">${waiting ? '取消排队' : '停止'}</button></div>`;
    if (!run.carousel_id && run.has_result && run.script_id) return `<div class="video-carousel-run"><p><strong>原逐张表已保留</strong></p><p class="context-note">${run.carousel_missing ? '关联的图文制作单已不存在，可查看保存的原表。' : '后续制作未完成，可查看保存的原表。'}${run.slide_count ? ` · ${run.slide_count} 张` : ''} · ${esc(readable(run.finished_at || run.started_at))}</p><a class="secondary compact" href="${esc(tableURL(run))}">查看原逐张表</a></div>`;
    if (run.status === 'needs_attention' && run.carousel_id) return `<div class="video-carousel-run"><p><strong>已建单，部分张需要人看</strong> ${esc(run.summary || '')}</p><a class="primary compact" href="${esc(carouselURL(run.carousel_id,run.product))}">打开图文制作单</a></div>`;
    if (run.status === 'completed' && run.carousel_id) return `<div class="video-carousel-run is-done"><p><strong>${run.autopilot && run.delivery_id ? '图文成品已完成' : '已转成图文'}</strong> ${esc(run.summary || '')}</p><p class="context-note">${esc(readable(run.finished_at))} · ${esc(run.video_source || '')}${run.frames ? ` · ${run.frames} 帧` : ''}</p>${run.problems?.length ? `<details><summary>需要留意（${run.problems.length}）</summary><ul>${run.problems.map(item => `<li>${esc(item)}</li>`).join('')}</ul></details>` : ''}${canDownload ? `<a class="primary compact" download href="/api/production/carousels/${encodeURIComponent(run.carousel_id)}/deliveries/${encodeURIComponent(run.delivery_id)}/download">下载成品包</a> ` : ''}<a class="${canDownload ? 'secondary' : 'primary'} compact" href="${esc(carouselURL(run.carousel_id,run.product))}">${canDownload ? '查看图文成品' : '打开图文制作单'}</a></div>`;
    return `<div class="video-carousel-run is-failed"><p><strong>${run.status === 'cancelled' ? '已停止' : run.carousel_id ? '这次制作未完成' : '这次没有转成'}</strong> ${esc(run.error || '')}</p><p class="context-note">${esc(readable(run.finished_at || run.started_at))}</p>${run.carousel_id ? `<p class="context-note">逐张表、候选画面和已完成图片已保留，可以从当前进度继续。</p><a class="primary compact" href="${esc(carouselURL(run.carousel_id,run.product))}">继续完成这组图文</a>` : ''}</div>`;
  }
  const readable = value => value ? String(value).replace('T',' ').slice(0,16) : '';
  // Registration and review times are stored in UTC; show them in local time, as the source fold does.
  const localTime = value => value ? window.DeliveryProvenance?.when?.(value) || readable(value) : '';
  function formMarkup(state,options) {
    const d = options.defaults || {}, name = id => options.models.find(m => m.id === id)?.name || id;
    const fallback = d.model ? `${name(d.model)}${d.reasoning_effort ? ` · ${effortNames[d.reasoning_effort] || d.reasoning_effort}` : ''}` : '本机自动选择';
    const provider = m => m.provider || (String(m.id).startsWith('claude-') ? 'claude' : 'codex');
    const groups = [['claude','Claude'],['codex','Codex']].map(([key,label]) => {
      const list = options.models.filter(m => provider(m) === key);
      return list.length ? `<optgroup label="${label}">${list.map(m => `<option value="${esc(m.id)}" ${m.available === false ? 'disabled' : ''}>${esc(m.name || m.id)}${m.available === false ? ' · 未就绪' : ''}</option>`).join('')}</optgroup>` : '';
    }).join('');
    const fromTimeline = state.source.kind === 'timeline', timelines = state.source.timelines || [];
    return `<form class="video-carousel-form"><p class="context-note">${state.source.available ? `成片：${esc(state.source.label)}。${fromTimeline ? '达芬奇忙时会排队等待；取帧时短暂占用（约一分钟），结束后自动切回你原来的时间线和位置。' : ''}` : '还没有可用的成片：先建达芬奇时间线，或填下面的本地成片路径，或在上面登记视频 ID（会从 TikTok 下载）。'}</p>
      ${fromTimeline && timelines.length > 1 ? `<label class="field"><span>从哪条时间线取帧</span><select name="timeline">${timelines.map(item => `<option value="${esc(item.unique_id)}" ${item.unique_id === state.source.timeline ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}</select></label>` : ''}
      <details class="video-publication-more" ${state.source.available ? '' : 'open'}><summary>${fromTimeline ? '不用达芬奇，改用成片文件（选填）' : '换一个成片文件（选填）'}</summary><label class="field"><span>本地成片完整路径</span><input name="video_path" maxlength="2000" placeholder="C:\\Videos\\最终成片.mp4"></label></details>
      <div class="form-row video-carousel-options"><label class="field"><span>张数</span><select name="slides"><option value="">AI 按内容定</option>${[3,4,5,6,7,8].map(n => `<option value="${n}">${n} 张</option>`).join('')}</select></label><label class="field"><span>模型</span><select name="model"><option value="">全局默认（${esc(fallback)}）</option>${groups}</select></label><label class="field"><span>思考强度</span><select name="effort" disabled><option value="">全局默认</option></select></label></div>
      <label class="field"><span>补充要求（选填）</span><textarea name="instruction" rows="2" maxlength="2000" placeholder="例如：首图用切纸那一下；最后一张放整套。"></textarea></label>
      <label class="ai-check"><input type="checkbox" name="autopilot" checked> AI 全程制作：选图、出图、核对并整理成品包</label><p class="context-note">取消勾选时，只写逐张表并放入候选帧，后续可自行调整。</p>
      <div class="video-publication-actions"><button type="submit" class="primary">AI 全程制作图文</button></div><p class="ai-error" data-video-carousel-error role="alert" hidden></p></form>`;
  }
  async function mountCarousel(box,{job,api}) {
    if (!box) return;
    const token = Symbol('video-carousel');box._token = token;
    clearTimeout(box._timer);
    let state;
    try {state = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/video-carousel`);}
    catch (error) {if (box._token === token) box.innerHTML = `<h3>转成图文</h3><p class="context-note">${esc(error.message || '暂时读不到转图文记录。')}</p>`;return;}
    if (!Array.isArray(state?.runs) || !state.source) {if (box._token === token) box.remove();return;}
    const options = await loadAiOptions(api);
    if (box._token !== token || !box.isConnected) return;
    const latest = state.runs[0], hasCarousel = Boolean(latest?.carousel_id);
    const older = state.runs.slice(1).filter(run => run.carousel_id || (run.has_result && run.script_id));
    box.innerHTML = `<h3>转成图文<details class="production-hint"><summary title="说明" aria-label="说明"></summary><div class="production-hint-body">工作台从成片取帧并去重，AI 按口播和画面写逐张表、挑选画面。默认由 AI 继续选图、出图、核对并整理成品包；完成后直接领取，也可在制作单里查看与调整。</div></details></h3>
      ${latest ? runCard(latest) : ''}
      ${older.length ? `<p class="context-note">之前转过：${older.map(run => `<a href="${esc(run.carousel_id ? carouselURL(run.carousel_id,run.product) : tableURL(run))}">${esc([run.carousel_id ? '图文制作单' : '原逐张表',!run.carousel_id && run.slide_count ? `${run.slide_count} 张` : '',readable(run.finished_at || run.started_at)].filter(Boolean).join(' · '))}</a>`).join('、')}</p>` : ''}
      ${state.running ? '' : `<details class="video-carousel-again" ${!hasCarousel && latest && ['failed','interrupted','cancelled','needs_attention'].includes(latest.status) ? 'open' : ''}><summary>${hasCarousel ? '再转一套' : '开始制作图文'}</summary>${formMarkup(state,options)}</details>`}`;
    box.querySelector('[data-video-carousel-stop]')?.addEventListener('click',async event => {
      event.currentTarget.disabled = true;
      try {await api(`/api/production/carousel-tables/${encodeURIComponent(event.currentTarget.dataset.videoCarouselStop)}/cancel`,{method:'POST',body:{}});}
      catch (error) {toast(error.message,true);}
      void mountCarousel(box,{job,api});
    });
    if (state.running) {box._timer = setTimeout(() => {if (box._token === token && box.isConnected) void mountCarousel(box,{job,api});},3000);return;}
    const form = box.querySelector('form');
    if (!form) return;
    const model = form.elements.model, effort = form.elements.effort;
    form.elements.autopilot.addEventListener('change',() => {form.querySelector('[type="submit"]').textContent = form.elements.autopilot.checked ? 'AI 全程制作图文' : '生成逐张表和候选帧';});
    model.addEventListener('change',() => {
      const list = (options.models.find(m => m.id === model.value)?.reasoning_efforts || []).filter(v => typeof v === 'string');
      effort.innerHTML = `<option value="">${model.value ? '模型默认' : '全局默认'}</option>` + list.map(v => `<option value="${esc(v)}">${esc(effortNames[v] || v)}</option>`).join('');
      effort.disabled = !model.value || !list.length;
    });
    form.addEventListener('submit',async event => {
      event.preventDefault();
      const error = form.querySelector('[data-video-carousel-error]'), button = form.querySelector('[type="submit"]');
      const values = Object.fromEntries(new FormData(form));
      const body = {request_id:crypto.randomUUID(),instruction:String(values.instruction || '').trim(),file_path:String(values.video_path || '').trim(),autopilot:form.elements.autopilot.checked};
      if (values.timeline) body.timeline = String(values.timeline);
      if (values.slides) body.slides = Number(values.slides);
      if (model.value) {body.model = model.value;body.effort = effort.value || null;}
      button.disabled = true;error.hidden = true;
      try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/video-carousel`,{method:'POST',body});toast(body.autopilot ? 'AI 已开始全程制作，完成后可直接领取成品。' : '已开始生成逐张表和候选帧。');void mountCarousel(box,{job,api});}
      catch (problem) {error.textContent = problem.message || '没能开始转图文。';error.hidden = false;button.disabled = false;}
    });
  }
  async function mount(host,{job,api,onSaved,onLoaded} = {}) {
    if (!host || !job || !api) return;
    const token = Symbol('video-publication');host._videoPublicationToken = token;
    host.classList.add('video-publication');
    host.innerHTML = '<h2>成片与发布</h2><p class="context-note">正在读取发布记录…</p>';
    let data;
    try {data = await api(base(job.id));}
    catch (error) {if (host._videoPublicationToken === token) host.innerHTML = `<h2>成片与发布</h2><p class="ai-error" role="alert">${esc(error.message || '发布记录暂时无法读取。')}</p><button type="button" class="secondary" data-retry-load>重新读取</button>`;host.querySelector('[data-retry-load]')?.addEventListener('click',() => mount(host,{job,api,onSaved,onLoaded}));return;}
    if (host._videoPublicationToken !== token || !host.isConnected) return;
    const item = data.publication, source = data.source || {};
    onLoaded?.(item || null);
    let savedRequest;
    try {savedRequest = requests.get(job.id) || JSON.parse(sessionStorage.getItem(storageKey(job.id)) || 'null');} catch {}
    const draft = savedRequest?.body || item || {};
    scriptPosts = (data.script_registrations || []).map(String);
    host.innerHTML = `<h2>成片与发布<details class="production-hint"><summary title="说明" aria-label="说明"></summary><div class="production-hint-body">登记发布后的视频 ID，并保存对应脚本、配音和人工审查版本。以后导入销售表时，会按这个 ID 自动关联到这张制作单和它的变体。成片文件可选绑定，绑定后会复制到工作台，随完整备份保存。</div></details></h2>
      ${item ? receipt(job,item,true) : ''}
      ${copyBlock(data.copy,{collapsed:Boolean(item)})}
      ${item ? '' : calendarPlanMarkup()}${item ? '' : data.can_register || savedRequest ? '' : '<p class="context-note">这张制作单尚未登记发布成片。</p>'}
      ${data.history?.length ? `<details class="video-publication-history"><summary>已绑定的其他视频与历次登记（${data.history.length} 条）</summary>${[...data.history].reverse().map(old => receipt(job,old)).join('')}</details>` : ''}
      ${data.can_register || savedRequest ? `<details class="video-publication-editor" ${!item || savedRequest ? 'open' : ''}><summary>${item ? '绑定另一个视频 / 修正当前登记' : '登记发布视频 ID'}</summary>
      <p class="context-note">${savedRequest ? '上次登记尚未确认，可用原请求恢复。' : `将绑定脚本 v${esc(source.script?.script_version)}、${esc(takeLabel(job,source.take))}、时间线 ${esc(source.timeline?.name || source.timeline?.unique_id)}。`}</p>${item || savedRequest ? '' : registrationHint(scriptPosts)}${item && !savedRequest ? '<p class="context-note">一张制作单可以绑定多个视频：填新的视频 ID 会再绑定一个，原登记保留；填原 ID 是修正当前登记。</p>' : ''}
      <form><div class="form-row video-publication-identity"><label class="field"><span>视频 ID（也可以直接粘贴视频链接）</span><input name="post_id" required maxlength="2000" inputmode="url" autocomplete="off" placeholder="7561234567890123456 或视频链接" value="${esc(draft.post_id || '')}"></label><input type="hidden" name="published_on" value="${esc(draft.published_on || localDate())}"><small data-account-hint></small></div>
      <details class="video-publication-more" ${draft.file_path || item?.file ? 'open' : ''}><summary>账号、帖子链接、备注与成片文件（都可不填）</summary><div class="form-row"><label class="field"><span>发布账号</span><input name="account" maxlength="200" list="video-publication-accounts" value="${esc(draft.account || '')}" autocomplete="off"><datalist id="video-publication-accounts"></datalist></label><label class="field"><span>帖子链接（不填按账号和 ID 生成）</span><input name="post_url" type="url" maxlength="2000" value="${esc(draft.post_url || '')}"></label></div><label class="field"><span>备注</span><textarea name="note" rows="2" maxlength="2000">${esc(draft.note || '')}</textarea></label>
      <label class="field"><span>绑定成片文件：本地完整路径</span><input name="file_path" maxlength="2000" placeholder="C:\\Videos\\最终成片.mp4" value="${esc(draft.file_path || item?.file?.original_path || '')}"></label>
      <label class="video-publication-confirm"><input name="file_confirmed" type="checkbox">我已核对：该文件就是以上审查版本实际发布的成片。</label></details>
      ${calendarChoice(draft.calendar_uploaded)}
      <div class="video-publication-actions"><button class="primary" type="submit">${savedRequest ? '恢复发布登记' : item ? '保存修正并保留原登记' : '保存发布登记'}</button><button class="secondary" type="button" data-refresh-publication>读取最新版本</button></div>
      <p class="context-note" data-save-status role="status"></p><p class="ai-error" data-publication-error role="alert" hidden></p></form></details>` : `<p class="context-note">${esc(data.blocked_reason)}</p>`}`;
    bindCopy(host,data.copy);
    host.querySelectorAll('[data-copy-take]').forEach(button => button.addEventListener('click',() => {
      if (typeof copyText === 'function') copyText(button.dataset.copyTake,'配音编号已复制');else navigator.clipboard.writeText(button.dataset.copyTake);
    }));
    const carouselBox = document.createElement('section');carouselBox.className = 'video-carousel';host.append(carouselBox);
    void mountCarousel(carouselBox,{job,api});
    host.querySelector('[data-refresh-publication]')?.addEventListener('click',() => {clearPending(job.id);mount(host,{job,api,onSaved,onLoaded});});
    host.querySelector('[data-calendar-toggle]')?.addEventListener('click',async event => {
      const button = event.currentTarget;button.disabled = true;
      try {
        await api(`${base(job.id)}/calendar`,{method:'POST',body:{publication_id:button.dataset.publicationId,calendar_uploaded:button.dataset.calendarToggle === '1'}});
        if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded});
      } catch (problem) {button.disabled = false;toast(problem.message || '没能更新发布日历状态。',true);}
    });
    const verify = host.querySelector('[data-verify-video]');
    if (verify) verify.addEventListener('click',async () => {
      verify.disabled = true;
      const message = host.querySelector('[data-verification]');message.textContent = '正在核对视频内容…';
      try {const result = await api(`${base(job.id)}/verify`,{method:'POST',body:{publication_id:item.id}});message.textContent = result.matches ? '视频内容与登记时一致。' : '视频内容与登记时不一致，请从备份恢复。';}
      catch (error) {message.textContent = error.message || '核对暂未完成。';}
      finally {verify.disabled = false;}
    });
    const form = host.querySelector('.video-publication-editor form');
    if (!item) void bindCalendarPlan(host,`video:${job.id}`,api,{form,after:onSaved});
    if (!form) return;
    const lockUncertain = () => {
      const uncertain = savedRequest && savedRequest.uncertain !== false;
      form.querySelectorAll('input,textarea,select,[data-refresh-publication]').forEach(node => node.disabled = Boolean(uncertain));
      if (uncertain) {
        form.elements.file_confirmed.checked = true;
        form.querySelector('[type="submit"]').textContent = '重试同一登记';
        form.querySelector('[data-save-status]').textContent = '上次结果尚未确认，输入已保留。请重试同一登记，完成后再修改。';
      }
    };
    lockUncertain();
    void assistIdentity(form,api);
    form.addEventListener('submit',async event => {
      event.preventDefault();
      if (form.dataset.busy) return;
      const values = normalize(Object.fromEntries(new FormData(form))), error = form.querySelector('[data-publication-error]');
      // A retry resends the saved request; its fields are locked, so there is nothing new to check.
      if (!(savedRequest && savedRequest.uncertain !== false)) {
        if (!/^\d{6,25}$/.test(values.post_id)) {error.textContent = '视频 ID 须为 6–25 位数字，或粘贴完整的视频链接。';error.hidden = false;return;}
        if (String(values.file_path || '').trim() && !form.elements.file_confirmed.checked) {error.textContent = '绑定成片文件时，请先勾选核对。';error.hidden = false;return;}
        if (values.account) try {localStorage.setItem('video-publication:account',values.account);} catch {}
      }
      const body = {...values,revision:data.revision,source_signature:data.source_signature,file_confirmed:form.elements.file_confirmed.checked,calendar_uploaded:values.calendar_uploaded === '1'};
      // A saved request remains exactly reproducible after a lost response or page reload.
      const old = savedRequest?.body;
      const sameInput = old && ['file_path','account','post_id','published_on','post_url','note','calendar_uploaded'].every(key => String(old[key] || '') === String(body[key] || ''));
      const request = (savedRequest && savedRequest.uncertain !== false) || sameInput ? old : pending(job.id,body);
      savedRequest = requests.get(job.id) || savedRequest;
      savedRequest.uncertain = true;remember(job.id,savedRequest);
      lockUncertain();
      form.dataset.busy = '1';error.hidden = true;
      form.querySelectorAll('button').forEach(button => button.disabled = true);
      form.querySelector('[data-save-status]').textContent = '正在保存发布登记…';
      try {await api(base(job.id),{method:'POST',body:request});clearPending(job.id);if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded});}
      catch (problem) {
        const definitive = Number.isInteger(problem.status) && problem.status >= 400 && problem.status < 500;
        savedRequest.uncertain = !definitive;remember(job.id,savedRequest);
        error.textContent = (problem.message || '结果尚未确认；重试会恢复同一登记。') + (definitive ? ' 可读取最新版本，核对现有登记后再修改。' : ' 输入已保留，请重试同一登记。');
        error.hidden = false;form.querySelector('[data-save-status]').textContent = '';
      }
      finally {delete form.dataset.busy;form.querySelectorAll('button').forEach(button => button.disabled = false);lockUncertain();}
    });
  }
  // The 图文 order's registration reuses the same fields and the same ID/link handling.
  return {mount,normalize,localDate,postedOn,followPostDate,copyBlock,bindCopy,calendarLine,calendarChoice,calendarPlanMarkup,bindCalendarPlan,registrationTag,registrationHint,tiktokUrl};
})();
