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
  // The state itself is the switch: one chip that reads 未上传/已上传 and flips on click.
  const uploadChip = (uploaded,attributes = '') => `<button type="button" class="video-publication-upload${uploaded ? ' is-uploaded' : ''}" aria-label="${uploaded ? '改回未上传' : '标记为已上传'}" title="${uploaded ? '已上传到发布日历，点一下改回未上传' : '点一下标记为已上传'}"${attributes}>${uploaded ? '已上传' : '未上传'}</button>`;
  function calendarLine(item,current,{inline = false} = {}) {
    const uploaded = item.calendar_uploaded;
    const state = current ? uploadChip(uploaded === true,` data-calendar-toggle="${uploaded === true ? '0' : '1'}" data-publication-id="${esc(item.id)}"`)
      : uploaded === true ? '<span class="script-status published">已上传</span>' : uploaded === false ? '<span class="script-status">未上传</span>' : '<small class="video-publication-unmarked" title="这条登记早于发布日历标记，没有记录是否上传">未记录上传</small>';
    return inline ? `<span class="video-publication-calendar">${state}</span>` : `<p class="video-publication-calendar">发布日历 ${state}</p>`;
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
    let current = {};
    const show = (state,keepDate = false) => {
      current = state;
      const day = state.calendar_date || localDate(), uploaded = state.calendar_uploaded === true;
      line.innerHTML = `<label class="video-publication-calendar-date">发布日期 <input type="date" value="${esc(day)}"></label> ${uploadChip(uploaded)}`;
      // The registration below carries the planned day as its posting day.
      const field = form?.elements.published_on;
      if (field && !keepDate && !form._postDate?.picked()) field.value = day;
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
    // A pasted post link proves the video is up: the registration form calls this to flip the mark without a click.
    if (form) form._markUploaded = async () => {
      if (current.calendar_uploaded === true || !line.querySelector('button')) return;
      const day = line.querySelector('input')?.value || current.calendar_date || localDate();
      try {await save({calendar_uploaded:true,calendar_date:day});show({...current,calendar_uploaded:true,calendar_date:day},true);await after?.();}
      catch (problem) {toast(problem.message || '没能更新发布日历状态。',true);}
    };
    try {show(await api(`${url}?id=${encodeURIComponent(contentId)}`));}
    catch {line.innerHTML = '发布日历 <span class="context-note">暂时读不到状态</span>';}
  }
  // One line says what was posted (account, day, post, script) with the calendar chip at its end; the frozen source sits under it.
  function receipt(job,item,current = false) {
    const jobId = job.id, source = item.source || {}, audio = source.take || {}, timeline = source.timeline || {};
    return `<article class="video-publication-receipt"><div class="video-publication-head"><strong>${esc(item.account)} · ${esc(item.published_on)}</strong>
      <span class="id-row">帖子 ${esc(item.post_id)}${typeof copyIdButton === 'function' ? copyIdButton(item.post_id,'视频 ID',{icon:true}) + openLinkButton(tiktokUrl(item,'video'),'原视频') : ''}${current ? '<button type="button" class="subtle compact" data-edit-post title="帖子 ID、账号或发布日期登记错了：直接改这条登记">修改</button>' : ''}</span><span class="video-publication-meta">脚本 v${esc(item.script_version)}${registrationTag(scriptPosts,item.post_id)}${item.file ? '' : '<small class="video-publication-aside" title="只登记了视频 ID，没有绑定成片文件">未绑定成片</small>'}${autoTag(item,current)}</span>${calendarLine(item,current,{inline:true})}</div>
      <p class="video-publication-sub"><span title="${esc(audio.id)}">${esc(takeLabel(job,audio,true))}</span> · ${esc(timeline.name || timeline.unique_id)}${source.manual_completion?.at ? ' · 人工完成' : source.edit ? ` · <span title="${esc(source.edit.id)}">AI 剪辑</span>` : ' · 完成方式未保留'}${item.file ? ` · ${esc(item.file.name)} · ${size(item.file.size)} <a href="${base(jobId)}/${encodeURIComponent(item.id)}/download">领取登记成片</a>` : ''}</p>
      ${current && !item.source_current ? '<p class="video-publication-warning">当前制作版本或审查状态已变化；这条发布记录仍保留当时的成片、配音和审查依据。</p>' : ''}
      ${current && item.file && !item.file_available ? '<p class="video-publication-warning">托管成片缺失或大小有变化，请从完整备份恢复。</p>' : ''}
      ${item.note ? `<p class="video-publication-sub">${esc(item.note)}</p>` : ''}
      ${current ? campaignReminder(item) : ''}
      ${window.DeliveryProvenance?.render({kind:'video',record:item,owner:job,copyTake:true,
        rows:`<dt>登记时间</dt><dd>${esc(localTime(item.registered_at))}</dd>${item.file ? `<dt>原成片路径</dt><dd>${esc(item.file.original_path)}</dd>` : ''}<dt>审查依据</dt><dd>${esc(localTime((source.manual_completion || source.edit?.approval)?.at) || '未保留')}</dd>`}) || ''}
      ${current && item.file ? '<button type="button" class="secondary" data-verify-video>核对保存的成片</button><p class="context-note" data-verification role="status"></p>' : ''}</article>`;
  }
  // A post that went up during a campaign: once the campaign is over its pinned comment and end card say something untrue.
  const shortDay = value => String(value || '').slice(5).replace('-','/');
  function campaignReminder(item) {
    const campaign = item.campaign;
    if (!campaign?.ended || item.pin_cleared) return '';
    return `<p class="video-publication-warning video-publication-campaign">这条在「${esc(campaign.name)}」期间发布，活动已于 ${esc(shortDay(campaign.end))} 结束：置顶评论如果写了活动请改掉，时间线上的活动屏显可在下方去掉。<button type="button" class="subtle compact" data-pin-cleared="${esc(item.id)}">已处理</button></p>`;
  }
  // Registered by the post matcher (TK 看板 push or a Shop export): the standing one can be taken back in one click.
  const autoTag = (item,current) => item.auto ? `<small class="video-publication-aside" title="${esc(`按发布文案标题自动对上（来源：${{tk_lens:'TK 看板',shop_export:'Shop 导出'}[item.auto.source] || '自动匹配'}）${item.auto.title ? `：${item.auto.title}` : ''}`)}">自动登记</small>${current ? '<button type="button" class="subtle compact" data-undo-auto title="不是这条视频：撤销登记，这个帖子以后不再自动登记到这条时间线">撤销</button>' : ''}` : '';
  // Posts that fit this timeline but were not registered by themselves (another order shares the title, or the account
  // has not posted for this creator before): one click registers, 不是 never offers it here again.
  function candidateRows(list) {
    if (!list?.length) return '';
    return `<div class="video-publication-candidates" role="group" aria-label="检测到的帖子">${list.map(post => `<div class="video-publication-candidate"><span class="context-note">检测到</span><strong>@${esc(post.account || '账号未知')} · ${esc(post.published_on)}</strong><span class="video-publication-candidate-title" title="${esc(post.title)}">${esc(String(post.title || '').split('#')[0].replace(/^\W+/,''))}</span>${post.also?.length ? `<small class="video-publication-aside" title="${esc(post.also.join('、'))}">也对得上另 ${post.also.length} 条</small>` : post.account_known ? '' : '<small class="video-publication-aside" title="这个达人还没有用这个账号登记过，请核对">账号待核对</small>'}<button type="button" class="primary compact" data-candidate-use="${esc(post.post_id)}">登记</button><button type="button" class="subtle compact" data-candidate-dismiss="${esc(post.post_id)}">不是</button></div>`).join('')}</div>`;
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
  // The video panel's copy is a short list in the order it is pasted at publish time: hook screen text, a title, then the text.
  // A line shows the foreign words with their Chinese under them; what gets copied is the foreign words alone.
  function copyRow(copy) {
    if (!copy || !(copy.titles?.length || copy.zh || copy.en || copy.screen_en || copy.screen_zh)) return '';
    const flat = value => String(value || '').replace(/\s*\n\s*/g,' ');
    const item = (attributes,main,under,hint) => `<button type="button" class="ai-title-line video-publish-copy-item" ${attributes} title="${esc(hint)}"><span><span class="video-publish-copy-main">${esc(main)}</span>${under}</span>${copyIcon()}</button>`;
    const reading = value => value ? `<small>${esc(value)}</small>` : '';
    const screen = copy.screen_en ? item('data-copy-screen',copy.screen_en,reading(copy.screen_zh),'复制外文屏显')
      : copy.screen_zh ? `<p class="video-publish-copy-note">${esc(copy.screen_zh)} <small>没有外文屏显可复制</small></p>` : '';
    const titles = (copy.titles || []).map((line,index) => item(`data-copy-title="${esc(line)}"`,line,`<small data-title-zh="${index}">${esc(copy.titles_zh?.[index] || '中文生成中…')}</small>`,line)).join('');
    const hasText = copy.zh || copy.en;
    const text = hasText ? `${item('data-copy-publish="text"',flat(copy.en || copy.zh),copy.en ? reading(flat(copy.zh)) : '','复制中文和外文文案')}<details class="video-publish-copy-fold"><summary>查看全文${copy.version ? ` · 脚本第 ${esc(copy.version)} 版` : ''}</summary><div class="video-publish-copy">${copy.zh ? `<p>${esc(copy.zh)}</p>` : ''}${copy.en ? `<p>${esc(copy.en)}</p>` : ''}</div></details>` : '';
    const line = (label,body) => body ? `<span class="video-publish-copy-label">${label}</span><div class="video-publish-copy-lines">${body}</div>` : '';
    return `<div class="video-publish-copy-row">${line('钩子屏显',screen)}${line('标题',titles)}${line('文案',text)}${line('置顶评论',pinLine(copy,item))}</div>`;
  }
  // The pinned comment carries the campaign, so the video itself never states a deadline that later runs out.
  function pinLine(copy,item) {
    const campaign = copy.campaign;
    if (!copy.pin || !campaign) return '';
    const when = campaign.status === 'running' ? `${campaign.name} 进行中，${shortDay(campaign.end)} 结束（太平洋时间）。先确认本品已报名，活动结束后改掉。`
      : `${campaign.name} ${shortDay(campaign.start)} 开始，活动期内发布再置顶。`;
    return item('data-copy-pin',copy.pin.en,`<small>${esc(copy.pin.zh ? `${copy.pin.zh} · ${when}` : when)}</small>`,'复制置顶评论');
  }
  // Titles are written without Chinese, so their reading is made on first opening and saved; copying never waits for it.
  const titleRuns = new Map();
  async function fillTitleChinese(host,job,api,copy) {
    if (!copy?.titles?.length || copy.titles_zh?.every(Boolean)) return;
    const run = titleRuns.get(job.id) || api(`${base(job.id)}/title-zh`,{method:'POST',body:{}}).finally(() => titleRuns.delete(job.id));
    titleRuns.set(job.id,run);
    let chinese = [];
    try {chinese = (await run).titles_zh || [];} catch {}
    host.querySelectorAll('[data-title-zh]').forEach(node => {
      const value = chinese[Number(node.dataset.titleZh)];
      if (value) node.textContent = value;
      else if (node.textContent === '中文生成中…') node.textContent = '中文对照暂时没生成';
    });
  }
  function bindCopy(host,copy) {
    const write = (button,text,message) => {
      if (typeof copyText === 'function') copyText(text,message); else navigator.clipboard.writeText(text);
      button.classList.add('copied');setTimeout(() => button.classList.remove('copied'),1200);
    };
    host.querySelectorAll('[data-copy-title]').forEach(button => button.addEventListener('click',() => {
      write(button,button.dataset.copyTitle,'标题与主题标签已复制');
    }));
    host.querySelectorAll('[data-copy-publish]').forEach(button => button.addEventListener('click',() => {
      // One button for the copy pasted at publish time: Chinese, a blank line, then English.
      write(button,[copy.zh,copy.en].filter(Boolean).join('\n\n'),copy.zh && copy.en ? '中英文案已复制' : '文案已复制');
    }));
    host.querySelectorAll('[data-copy-screen]').forEach(button => button.addEventListener('click',() => {
      write(button,copy.screen_en,'屏显外文已复制');
    }));
    host.querySelectorAll('[data-copy-pin]').forEach(button => button.addEventListener('click',() => {
      write(button,copy.pin?.en || '','置顶评论已复制');
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
    // A valid post link or ID means the post is up, so the registration carries 已上传 and the order's calendar mark follows.
    const markUploaded = () => {
      const value = String(field.value || '').trim();
      if (!fromLink(value) && !/^\d{15,25}$/.test(value)) return;
      if (form.elements.calendar_uploaded) form.elements.calendar_uploaded.value = '1';
      void form._markUploaded?.();
    };
    field.addEventListener('input',sync);
    field.addEventListener('input',markUploaded);
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
  const readable = value => value ? String(value).replace('T',' ').slice(0,16) : '';
  // Registration and review times are stored in UTC; show them in local time, as the source fold does.
  const localTime = value => value ? window.DeliveryProvenance?.when?.(value) || readable(value) : '';
  function historyFold(job,history,current) {
    if (!history?.length) return '';
    const records = [...history,...(current ? [current] : [])];
    const day = value => localTime(value).slice(0,10) || '日期未保留';
    const rows = [...history].reverse().map(old => {
      const next = records.slice(records.indexOf(old) + 1).find(record => record.post_id === old.post_id);
      if (!next) return receipt(job,old);
      const post = current && old.post_id === current.post_id ? '' : `帖子 ${esc(old.post_id)} · `;
      return `<details class="video-publication-superseded"><summary>${post}${esc(day(old.registered_at))} 登记 · 发布日期 ${esc(old.published_on)} · 已被 ${esc(day(next.registered_at))} 的登记取代</summary>${receipt(job,old)}</details>`;
    });
    const others = rows.filter(row => row.startsWith('<article')).length, versions = rows.length - others;
    return `<details class="video-publication-history"><summary>${[others ? `其他视频 ${others} 个` : '',versions ? `旧版登记 ${versions} 条` : ''].filter(Boolean).join(' · ')}</summary>${rows.join('')}</details>`;
  }
  // Only orders with several timelines get the switch; each button carries that timeline's state.
  function timelinePicker(lines,chosen) {
    if (!lines || lines.length < 2) return '';
    const state = line => line.registered ? `已登记${line.source_current ? '' : '（版本已变）'}` : line.can_register ? '待登记' : '待完成审查';
    return `<div class="video-publication-lines" role="group" aria-label="登记哪条时间线的发布">${lines.map(line => `<button type="button" class="video-publication-line${line.unique_id === chosen ? ' is-current' : ''}" data-publication-timeline="${esc(line.unique_id)}" aria-pressed="${line.unique_id === chosen}"${line.post_id ? ` title="帖子 ${esc(line.post_id)}"` : ''}>${esc(line.name || line.unique_id)} <small>${esc(state(line))}</small></button>`).join('')}</div>`;
  }
  // The chosen timeline's own tools, ahead of its registration: copy its name, open it in Resolve, check its subtitles.
  function subtitleResult(timeline) {
    const check = timeline.subtitle_check;
    if (!check) return '';
    const seconds = frame => `${(frame / Number(timeline.fps)).toFixed(1)} 秒`;
    const rows = [...(check.changes || []).map(row => `<li>${seconds(row.start)}：${esc(row.before)} → <strong>${esc(row.after)}</strong></li>`),
      ...(check.review || []).map(row => `<li>${seconds(row.start)}：${esc(row.text)}<small>${!row.wrote ? `字幕里没有“${esc(row.expected)}”` : row.expected ? `字幕写的是“${esc(row.wrote)}”，稿子是“${esc(row.expected)}”` : `字幕多了“${esc(row.wrote)}”，稿子里没有`}，请你确认</small></li>`)];
    return `<p class="video-publication-subtitles" data-subtitle-state="${esc(check.status)}"><span class="script-status${['clean','fixed'].includes(check.status) ? ' published' : ''}">${esc(check.message)}</span> <small>${esc(localTime(check.at))}</small></p>${rows.length ? `<details class="video-publication-subtitle-details"><summary>查看 ${rows.length} 处</summary><ul>${rows.join('')}</ul></details>` : ''}`;
  }
  function hookResult(timeline) {
    const hook = timeline.hook_overlay;
    if (!hook) return '';
    return `<p class="video-publication-subtitles" data-hook-state="${esc(hook.status)}"><span class="script-status${hook.status === 'saved' ? ' published' : ''}">${esc(hook.message)}</span> ${esc(hook.text)} <small>${esc(localTime(hook.at))}</small></p>`;
  }
  // The script's hooks to choose from: the recommended one first, then its alternatives.
  function hookOptions(copy) {
    const written = copy?.screen_options?.length ? copy.screen_options : copy?.screen_en ? [{label:'',en:copy.screen_en,zh:copy.screen_zh}] : [];
    return written.filter(option => option.en.trim() && !option.en.trim().startsWith('无'));
  }
  const hookWords = value => String(value || '').trim().replace(/\s+\/\s*/g,' / ').replace(/ \/ $/,'');
  const hookTitle = (lock,text) => lock || (text ? `在开头 3 秒加上：${text}` : '这张制作单还没有开头屏显，可以选“自己写”。');
  // The campaign's end card: only while a trusted campaign runs, on a track of its own so it comes off again afterwards.
  function campaignTools(timeline,copy,lock) {
    const card = timeline.campaign_overlay, onTimeline = card && card.status !== 'removed', campaign = copy?.campaign;
    if (!onTimeline && !campaign) return '';
    const running = campaign?.status === 'running';
    const why = lock || (running ? `在结尾 3 秒加上活动屏显，口播不动；${shortDay(campaign.end)} 活动结束后记得去掉。` : campaign ? `${campaign.name} ${shortDay(campaign.start)} 开始后才能加。` : '');
    const field = onTimeline ? `<span>${esc(card.text)}</span>` : `<input type="text" maxlength="300" data-campaign-text aria-label="活动屏显文字" value="${esc(copy.card_text || '')}" ${running && !lock ? '' : 'disabled'}>`;
    const button = onTimeline ? `<button type="button" class="secondary compact" data-campaign-card="remove" ${lock ? 'disabled' : ''}>去掉活动屏显</button>`
      : `<button type="button" class="secondary compact" data-campaign-card="add" ${running && !lock ? '' : 'disabled'} title="${esc(why)}">加活动屏显</button>`;
    return `<div class="video-publication-campaign-card"><span class="context-note">活动屏显</span>${field}${button}${card?.message ? `<small>${esc(card.message)}</small>` : ''}</div>`;
  }
  function timelineTools(job,uid,copy) {
    const timeline = (job.timelines || []).find(row => row.unique_id === uid);
    if (!timeline) return '';
    const running = (job.edits || []).some(edit => edit.timeline_uid === uid && edit.status === 'running');
    const lock = running ? 'AI 正在剪辑，结束后可核对字幕。' : job.archived ? '恢复制作单后可核对字幕。' : '';
    const hookLock = running ? 'AI 正在剪辑，结束后可加屏显。' : job.archived ? '恢复制作单后可加屏显。' : '';
    const options = hookOptions(copy), onTimeline = timeline.hook_overlay?.text || '';
    const picked = onTimeline ? options.findIndex(option => hookWords(option.en) === hookWords(onTimeline)) : options.length ? 0 : -1;
    const hookText = picked < 0 ? onTimeline : options[picked].en;
    return `<div class="video-publication-timeline" data-timeline-tools="${esc(uid)}"><div class="video-publication-timeline-bar"><span class="id-row"><strong>${esc(timeline.name)}</strong>${typeof copyIdButton === 'function' ? copyIdButton(timeline.name,'时间线名称',{icon:true}) : ''}</span><button type="button" class="secondary compact" data-open-timeline>在达芬奇打开</button><button type="button" class="secondary compact" data-check-subtitles ${lock ? 'disabled' : ''} title="${esc(lock || '和配音稿对比，用词错误直接改好，拿不准的列出来。')}">核对字幕</button><button type="button" class="secondary compact" data-add-hook data-hook-lock="${esc(hookLock)}" ${hookLock || !hookText ? 'disabled' : ''} title="${esc(hookTitle(hookLock,hookText))}">${timeline.hook_overlay ? '更新屏显' : '加屏显'}</button><button type="button" class="secondary compact" data-download-timeline title="下载这条时间线的 .drt 和它用的配音（不含视频素材），在另一台有同样素材的电脑上接着剪。">下载时间线</button><button type="button" class="secondary compact" data-home-timeline ${lock ? 'disabled' : ''} title="${esc(lock ? lock.replace('核对字幕','替换') : '上传另一台电脑在达芬奇导出的 .drt，导入成新时间线并接上这台电脑的素材；原时间线归档保留，可恢复。')}">换成家里剪的</button><input type="file" accept=".drt" data-home-timeline-file hidden></div>${importNote(timeline)}<div class="video-publication-hook-choice"><select data-hook-choice aria-label="选屏显文字" ${hookLock ? 'disabled' : ''}>${options.map((option,index) => `<option value="${index}" ${index === picked ? 'selected' : ''}>${esc(option.label ? `${option.label}：` : '')}${esc(option.en)}</option>`).join('')}<option value="custom" ${picked < 0 ? 'selected' : ''}>自己写…</option></select><input type="text" maxlength="300" data-hook-custom aria-label="自己写的屏显文字" placeholder="第一行 / 第二行" value="${esc(picked < 0 ? onTimeline : '')}" ${picked < 0 ? '' : 'hidden'} ${hookLock ? 'disabled' : ''}><small data-hook-reading>${esc(options[picked]?.zh || '')}</small></div>${campaignTools(timeline,copy,hookLock)}${subtitleResult(timeline)}${hookResult(timeline)}<p class="ai-error" data-timeline-tools-error role="alert" hidden></p></div>`;
  }
  function importNote(timeline) {
    const from = timeline.imported_from;if (!from) return '';
    const warnings = timeline.warnings || [];
    return `<p class="context-note">从另一台电脑导入，替换 ${esc(from.timeline_name)}${from.clips ? `，${from.clips} 个片段都接上了素材` : ''}。${warnings.map(esc).join(' ')}</p>`;
  }
  const fileBase64 = file => new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(new Error('读不了这个文件，请重新选择。'));
    reader.readAsDataURL(file);
  });
  function bindTimelineTools(host,job,api,onTimelineChanged,copy) {
    const tools = host.querySelector('[data-timeline-tools]');
    if (!tools) return;
    const uid = tools.dataset.timelineTools, url = `/api/production/jobs/${encodeURIComponent(job.id)}`;
    const open = tools.querySelector('[data-open-timeline]'), check = tools.querySelector('[data-check-subtitles]'), hook = tools.querySelector('[data-add-hook]'), error = tools.querySelector('[data-timeline-tools-error]');
    const problem = failure => {
      const message = failure.message || '这次操作没有完成，请重试。';
      error.textContent = message;error.hidden = false;toast(message,true);
    };
    const repaint = async timeline => {
      Object.assign((job.timelines || []).find(row => row.unique_id === uid) || {},timeline);
      // Only this row repaints, so a video ID being typed below stays. The panel may have moved to another timeline meanwhile.
      const shown = host.querySelector('[data-timeline-tools]');
      if (shown?.dataset.timelineTools === uid) {shown.outerHTML = timelineTools(job,uid,copy);bindTimelineTools(host,job,api,onTimelineChanged,copy);}
      // The result moved the order's revision, which the timeline step saves against.
      try {await onTimelineChanged?.();} catch {}
    };
    open.addEventListener('click',async () => {
      open.disabled = true;error.hidden = true;
      try {toast(`已在达芬奇打开 ${(await api(`${url}/open`,{method:'POST',body:{unique_id:uid}})).name}。`);}
      catch (failure) {problem(failure);}
      finally {open.disabled = false;}
    });
    check.addEventListener('click',async () => {
      check.disabled = true;check.textContent = '核对中…';error.hidden = true;
      let result;
      try {result = await api(`${url}/subtitles`,{method:'POST',body:{timeline_uid:uid,request_id:crypto.randomUUID()}});}
      catch (failure) {problem(failure);check.disabled = false;check.textContent = '核对字幕';return;}
      const outcome = result.timeline.subtitle_check;
      toast(outcome.message,!['clean','fixed'].includes(outcome.status));
      await repaint(result.timeline);
    });
    const download = tools.querySelector('[data-download-timeline]');
    download.addEventListener('click',async () => {
      download.disabled = true;download.textContent = '导出中…';error.hidden = true;
      try {
        const response = await fetch(`${url}/timeline-file?uid=${encodeURIComponent(uid)}`,{credentials:'same-origin',cache:'no-store'});
        if (!response.ok) {const result = await response.json().catch(() => ({}));throw new Error(result.error || '下载失败，请重新登录后再试。');}
        const name = `${(job.timelines || []).find(row => row.unique_id === uid)?.name || 'timeline'}.zip`;
        const link = URL.createObjectURL(await response.blob()), anchor = document.createElement('a');
        anchor.href = link;anchor.download = name;anchor.click();setTimeout(() => URL.revokeObjectURL(link),1000);
        toast(`已下载 ${name}：里面是时间线和配音。解压后先把配音拖进达芬奇媒体池，再导入 .drt。`);
      } catch (failure) {problem(failure);}
      finally {download.disabled = false;download.textContent = '下载时间线';}
    });
    const home = tools.querySelector('[data-home-timeline]'), homeFile = tools.querySelector('[data-home-timeline-file]');
    home.addEventListener('click',() => {homeFile.value = '';homeFile.click();});
    homeFile.addEventListener('change',async () => {
      const file = homeFile.files[0];if (!file) return;
      if (!/\.drt$/i.test(file.name)) {problem(new Error('请选择达芬奇导出的 .drt 时间线文件。'));return;}
      if (file.size > 3 * 1024 * 1024) {problem(new Error('时间线文件超过 3 MB；只导出时间线，不要带素材。'));return;}
      home.disabled = true;home.textContent = '导入中…';error.hidden = true;
      let result;
      try {result = await api(`${url}/home-timeline`,{method:'POST',body:{timeline_uid:uid,request_id:crypto.randomUUID(),file_name:file.name,data:await fileBase64(file)}});}
      catch (failure) {problem(failure);home.disabled = false;home.textContent = '换成家里剪的';return;}
      const warnings = result.timeline.warnings || [];
      toast(`已导入 ${result.timeline.name}，原时间线已归档。${warnings.join(' ')}`,warnings.length > 0);
      try {await onTimelineChanged?.();} catch {}
    });
    const choice = tools.querySelector('[data-hook-choice]'), custom = tools.querySelector('[data-hook-custom]'), reading = tools.querySelector('[data-hook-reading]');
    const options = hookOptions(copy);
    const chosenText = () => choice.value === 'custom' ? custom.value.trim() : options[Number(choice.value)].en;
    const chosen = () => {
      custom.hidden = choice.value !== 'custom';
      reading.textContent = choice.value === 'custom' ? '' : options[Number(choice.value)].zh || '';
      hook.disabled = Boolean(hook.dataset.hookLock) || !chosenText();
      hook.title = hookTitle(hook.dataset.hookLock,chosenText());
    };
    tools.querySelector('[data-campaign-card]')?.addEventListener('click',async event => {
      const button = event.currentTarget, action = button.dataset.campaignCard, label = button.textContent;
      button.disabled = true;button.textContent = '处理中…';error.hidden = true;
      let result;
      try {result = await api(`${url}/campaign`,{method:'POST',body:{timeline_uid:uid,request_id:crypto.randomUUID(),action,text:tools.querySelector('[data-campaign-text]')?.value.trim() || ''}});}
      catch (failure) {problem(failure);button.disabled = false;button.textContent = label;return;}
      const outcome = result.timeline.campaign_overlay;
      toast(outcome.message,!['saved','removed'].includes(outcome.status));
      await repaint(result.timeline);
    });
    choice.addEventListener('change',() => {chosen();if (!custom.hidden) custom.focus();});
    custom.addEventListener('input',chosen);
    hook.addEventListener('click',async () => {
      const label = hook.textContent;
      hook.disabled = true;hook.textContent = '处理中…';error.hidden = true;
      let result;
      try {result = await api(`${url}/hook`,{method:'POST',body:{timeline_uid:uid,request_id:crypto.randomUUID(),text:chosenText()}});}
      catch (failure) {problem(failure);hook.disabled = false;hook.textContent = label;return;}
      const outcome = result.timeline.hook_overlay;
      toast(outcome.message,outcome.status !== 'saved');
      await repaint(result.timeline);
    });
  }
  const previewMarkup = uid => `<section class="timeline-preview" data-timeline-preview="${esc(uid)}"><div class="timeline-preview-heading"><h3>视频预览</h3><button type="button" class="secondary compact" data-preview-import>从达芬奇更新预览</button><span class="context-note" data-preview-progress role="status"></span></div><p class="timeline-preview-hint" data-preview-empty>人工确认完成后自动导出成片，也可手动更新半成品预览；保留字幕、屏显和声音。</p><div data-preview-video hidden><p class="timeline-preview-caption"><strong data-preview-kind></strong><span data-preview-name></span><time data-preview-time></time><a data-preview-download download>下载高清视频</a></p><video controls playsinline preload="auto" aria-label="半成品或成片视频预览"></video></div><p class="ai-error" data-preview-error role="alert" hidden></p></section>`;
  async function bindPreview(host,job,api,uid) {
    const root = host.querySelector('[data-timeline-preview]');if (!root) return;
    const button = root.querySelector('[data-preview-import]'), progress = root.querySelector('[data-preview-progress]');
    const error = root.querySelector('[data-preview-error]'), box = root.querySelector('[data-preview-video]'), player = box.querySelector('video');
    const url = `/api/production/jobs/${encodeURIComponent(job.id)}/preview`;
    let timer, version = 0, posting = false;
    const fail = problem => {error.textContent = problem.message || '预览暂时无法读取，请重试。';error.hidden = false;};
    const show = data => {
      if (!root.isConnected) return;
      clearTimeout(timer);
      const busy = data.status === 'running' || data.status === 'optimizing';
      button.disabled = busy || Boolean(data.blocked_reason);button.setAttribute('aria-busy',String(busy));
      button.textContent = data.status === 'optimizing' ? '正在准备播放…' : busy ? data.automatic ? '正在自动导出成片…' : '正在导出预览…' : '从达芬奇更新预览';button.title = data.blocked_reason || '';
      progress.textContent = (busy ? data.message || '正在导出…' : data.blocked_reason || data.message || '') +
        (busy && Number.isFinite(data.percent) ? ` ${Math.max(0,Math.min(100,data.percent))}%` : '');
      error.hidden = data.status !== 'failed';if (data.status === 'failed') error.textContent = data.error;
      const video = data.video;box.hidden = !video;root.querySelector('[data-preview-empty]').hidden = Boolean(video);
      if (video) {
        if (player.getAttribute('src') !== video.url) {
          const generation = video.source_version || video.version;
          const resume = player.dataset.generation === generation && player.currentTime > 0;
          const position = player.currentTime, playing = !player.paused;
          player.pause();player.dataset.generation = generation;
          if (resume) player.addEventListener('loadedmetadata',() => {
            if (player.getAttribute('src') !== video.url) return;
            player.currentTime = Math.min(position,Number.isFinite(player.duration) ? player.duration : position);
            if (playing) void player.play().catch(() => {});
          },{once:true});
          player.src = video.url;
        }
        root.querySelector('[data-preview-kind]').textContent = video.kind || '视频预览';
        root.querySelector('[data-preview-name]').textContent = video.name || '';
        root.querySelector('[data-preview-time]').textContent = video.generated_at ? `更新于 ${localTime(video.generated_at)}` : '';
        const download = root.querySelector('[data-preview-download]');
        download.href = video.download_url || video.url;download.download = video.name || '成片.mp4';
        download.textContent = video.kind === '成片预览' ? '下载高清成片' : '下载高清视频';
      }
      if (busy) timer = setTimeout(load,2000);
    };
    const load = async () => {
      if (!root.isConnected) return;
      const current = ++version;
      try {const data = await api(`${url}?timeline=${encodeURIComponent(uid)}`);if (current === version) show(data);}
      catch (problem) {if (current === version && root.isConnected) {fail(problem);timer = setTimeout(load,3000);}}
    };
    player.onerror = () => fail(new Error('这份预览暂时无法播放，可以从达芬奇重新更新。'));
    button.onclick = async () => {
      if (posting || button.disabled) return;
      posting = true;button.disabled = true;error.hidden = true;clearTimeout(timer);
      const current = ++version;
      try {const data = await api(url,{method:'POST',body:{timeline_uid:uid}});if (current === version) show(data);}
      catch (problem) {if (root.isConnected) {button.disabled = false;fail(problem);}}
      finally {posting = false;}
    };
    await load();
  }
  async function mount(host,{job,api,onSaved,onLoaded,onTimelineChanged,picker,onLines,onRegistered} = {}) {
    if (!host || !job || !api) return;
    // A page with its own timeline selector hides this panel's switch and drives it through select().
    if (picker !== undefined) host._videoPublicationPicker = picker;
    if (onLines) host._videoPublicationOnLines = onLines;
    if (onRegistered) host._videoPublicationOnRegistered = onRegistered;
    host._videoPublicationArgs = {job,api,onSaved,onLoaded,onTimelineChanged};
    const token = Symbol('video-publication');host._videoPublicationToken = token;
    host.classList.add('video-publication');
    host.innerHTML = '<h2>成片与发布</h2><p class="context-note">正在读取发布记录…</p>';
    let data;
    try {data = await api(base(job.id) + (host._videoPublicationTimeline ? `?timeline=${encodeURIComponent(host._videoPublicationTimeline)}` : ''));}
    catch (error) {if (host._videoPublicationToken === token) host.innerHTML = `<h2>成片与发布</h2><p class="ai-error" role="alert">${esc(error.message || '发布记录暂时无法读取。')}</p><button type="button" class="secondary" data-retry-load>重新读取</button>`;host.querySelector('[data-retry-load]')?.addEventListener('click',() => mount(host,{job,api,onSaved,onLoaded,onTimelineChanged}));return;}
    if (host._videoPublicationToken !== token || !host.isConnected) return;
    const item = data.publication, source = data.source || {};
    // One registration stands for each timeline. With several, a saved-but-unconfirmed request is kept per timeline, and the
    // order's header follows the current timeline only, never the one just being looked at.
    const lines = data.timelines || [], multi = lines.length > 1, key = multi ? `${job.id}:${data.timeline_uid}` : job.id;
    host._videoPublicationShown = data.timeline_uid;host._videoPublicationLines = lines;host._videoPublicationOnLines?.(lines,data.timeline_uid);
    if (!multi || !host._videoPublicationTimeline || host._videoPublicationTimeline === lines[0].unique_id) onLoaded?.(item || null);
    let savedRequest;
    try {savedRequest = requests.get(key) || JSON.parse(sessionStorage.getItem(storageKey(key)) || 'null');} catch {}
    const draft = savedRequest?.body || item || {};
    scriptPosts = (data.script_registrations || []).map(String);
    const canEdit = data.can_register || savedRequest;
    // What the 登记 button binds (and which registration of the script this is) shows on hover instead of as standing notes.
    const binds = `将绑定脚本 v${source.script?.script_version ?? ''}、${takeLabel(job,source.take)}、时间线 ${source.timeline?.name || source.timeline?.unique_id || ''}。${!item && scriptPosts.length ? `此脚本已登记 ${scriptPosts.length} 次，本次将是第 ${scriptPosts.length + 1} 次登记。` : ''}`;
    // One bar: (planned day and upload chip,) the video ID and the button; everything optional folds beneath it.
    const formMarkup = lead => `<form class="video-publication-form"><div class="video-publication-bar">${lead}<input name="post_id" required maxlength="2000" inputmode="url" autocomplete="off" aria-label="视频 ID（也可以直接粘贴视频链接）" placeholder="视频 ID 或视频链接" value="${esc(draft.post_id || '')}"><button class="primary compact" type="submit" title="${esc(binds)}">${savedRequest ? '恢复发布登记' : item ? '保存' : '登记'}</button></div>
      <input type="hidden" name="published_on" value="${esc(draft.published_on || localDate())}">${calendarChoice(draft.calendar_uploaded)}
      <small data-account-hint></small><p class="context-note" data-save-status role="status">${savedRequest ? '上次登记尚未确认，可用原请求恢复。' : ''}</p><p class="ai-error" data-publication-error role="alert" hidden></p><button class="subtle compact" type="button" data-refresh-publication hidden>读取最新版本</button>
      <details class="video-publication-more" ${draft.file_path || item?.file ? 'open' : ''}><summary>账号、帖子链接、备注与成片文件</summary><div class="form-row"><label class="field"><span>发布账号</span><input name="account" maxlength="200" list="video-publication-accounts" value="${esc(draft.account || '')}" autocomplete="off"><datalist id="video-publication-accounts"></datalist></label><label class="field"><span>帖子链接（不填按账号和 ID 生成）</span><input name="post_url" type="url" maxlength="2000" value="${esc(draft.post_url || '')}"></label></div><label class="field"><span>备注</span><textarea name="note" rows="2" maxlength="2000">${esc(draft.note || '')}</textarea></label>
      <label class="field"><span>绑定成片文件：本地完整路径</span><input name="file_path" maxlength="2000" placeholder="C:\Videos\最终成片.mp4" value="${esc(draft.file_path || item?.file?.original_path || '')}"></label>
      <label class="video-publication-confirm"><input name="file_confirmed" type="checkbox">我已核对：该文件就是以上审查版本实际发布的成片。</label></details></form>`;
    host.innerHTML = `<h2>成片与发布<details class="production-hint"><summary title="说明" aria-label="说明"></summary><div class="production-hint-body">登记发布后的视频 ID，并保存对应脚本、配音和人工审查版本。导入或同步销售视频表时，能唯一匹配的帖子会自动登记；匹配结果不唯一的帖子会列在这里，点一下登记。自动登记可以撤销。发布后在浏览器打开自己账号的主页，TK 看板也会把新帖推给工作台。成片文件可选绑定，绑定后会复制到工作台，随完整备份保存。</div></details></h2>
      ${host._videoPublicationPicker === false ? '' : timelinePicker(lines,data.timeline_uid)}
      ${lines.some(line => line.unique_id === data.timeline_uid) ? timelineTools(job,data.timeline_uid,data.copy) : ''}
      ${data.timeline_uid ? previewMarkup(data.timeline_uid) : ''}
      ${data.timeline_uid && window.NotionUpload ? window.NotionUpload.markup(data.timeline_uid) : ''}
      ${data.timeline_uid && window.PhotoCarousel ? window.PhotoCarousel.markup(data.timeline_uid) : ''}
      ${item ? receipt(job,item,true) + copyRow(data.copy) : canEdit ? candidateRows(data.candidates) + copyRow(data.copy) + formMarkup(calendarPlanMarkup()) : `<div class="video-publication-bar">${calendarPlanMarkup()}<span class="context-note">${esc(data.blocked_reason)}</span></div>${copyRow(data.copy)}`}
      <div class="video-publication-folds">
      ${item && !canEdit ? `<p class="context-note">${esc(data.blocked_reason)}</p>` : ''}
      ${historyFold(job,data.history,item)}
      </div>`;
    bindCopy(host,data.copy);
    bindTimelineTools(host,job,api,onTimelineChanged,data.copy);
    if (data.timeline_uid) void bindPreview(host,job,api,data.timeline_uid);
    if (window.NotionUpload && data.timeline_uid) void window.NotionUpload.bind(host,{job,api,uid:data.timeline_uid});
    if (window.PhotoCarousel && data.timeline_uid) void window.PhotoCarousel.bind(host,{job,api,uid:data.timeline_uid});
    void fillTitleChinese(host,job,api,data.copy);
    host.querySelectorAll('[data-publication-timeline]').forEach(button => button.addEventListener('click',() => {
      if (button.classList.contains('is-current')) return;
      host._videoPublicationTimeline = button.dataset.publicationTimeline;
      void mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});
    }));
    host.querySelectorAll('[data-copy-take]').forEach(button => button.addEventListener('click',() => {
      if (typeof copyText === 'function') copyText(button.dataset.copyTake,'配音编号已复制');else navigator.clipboard.writeText(button.dataset.copyTake);
    }));
    host.querySelector('[data-refresh-publication]')?.addEventListener('click',() => {clearPending(key);mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});});
    host.querySelector('[data-calendar-toggle]')?.addEventListener('click',async event => {
      const button = event.currentTarget;button.disabled = true;
      try {
        await api(`${base(job.id)}/calendar`,{method:'POST',body:{publication_id:button.dataset.publicationId,calendar_uploaded:button.dataset.calendarToggle === '1'}});
        if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});
      } catch (problem) {button.disabled = false;toast(problem.message || '没能更新发布日历状态。',true);}
    });
    host.querySelector('[data-pin-cleared]')?.addEventListener('click',async event => {
      const button = event.currentTarget;button.disabled = true;
      try {
        await api(`${base(job.id)}/pin`,{method:'POST',body:{publication_id:button.dataset.pinCleared,pin_cleared:true}});
        if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});
      } catch (problem) {button.disabled = false;toast(problem.message || '没能标记。',true);}
    });
    // Correct the standing registration in place; the frozen script, voiceover, timeline and file stay as registered.
    host.querySelector('[data-edit-post]')?.addEventListener('click',event => {
      const receiptNode = event.currentTarget.closest('.video-publication-receipt');
      if (receiptNode.querySelector('[data-post-editor]')) return;
      // One box: paste the TikTok link and the post ID, account and day all come from it. A bare ID keeps the account.
      const editor = document.createElement('form');editor.className = 'video-publication-post-editor';editor.dataset.postEditor = '';
      editor.innerHTML = `<label class="field"><span>粘贴正确的 TikTok 链接</span><input name="link" required maxlength="2000" placeholder="https://www.tiktok.com/@账号/video/…" autocomplete="off"></label><div class="video-publication-post-editor-actions"><button type="submit" class="primary compact" disabled>保存修改</button><button type="button" class="secondary compact" data-cancel-edit>取消</button></div><p class="context-note" data-edit-preview hidden></p><p class="ai-error" role="alert" hidden></p>`;
      receiptNode.querySelector('.video-publication-head').after(editor);
      const input = editor.elements.link, save = editor.querySelector('[type="submit"]'), preview = editor.querySelector('[data-edit-preview]'), error = editor.querySelector('.ai-error');
      input.focus();
      editor.querySelector('[data-cancel-edit]').onclick = () => editor.remove();
      const read = () => {
        const linked = fromLink(input.value), post = linked?.post_id || (/^\s*\d{6,25}\s*$/.test(input.value) ? input.value.trim() : '');
        if (!post) return null;
        // The same post keeps the day it was registered with; another post takes the day its ID was posted.
        return {post_id:post,account:linked?.account || item.account || '',published_on:post === item.post_id ? item.published_on : postedOn(post) || item.published_on,...(linked ? {post_url:linked.post_url} : {})};
      };
      input.addEventListener('input',() => {
        const next = read();error.hidden = true;save.disabled = !next;preview.hidden = !next;
        if (next) preview.textContent = `将改为：${next.account ? '@' + next.account + ' · ' : ''}帖子 ${next.post_id} · ${next.published_on}`;
      });
      editor.addEventListener('submit',async submit => {
        submit.preventDefault();
        const next = read();
        if (!next) {error.textContent = '请粘贴 TikTok 视频链接，或 6–25 位帖子 ID。';error.hidden = false;return;}
        save.disabled = true;error.hidden = true;
        try {
          await api(`${base(job.id)}/edit`,{method:'POST',body:{publication_id:item.id,revision:data.revision,...next}});
          toast('已修改这条发布登记。');
          if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});
        } catch (problem) {save.disabled = false;error.textContent = problem.message || '没能修改登记。';error.hidden = false;}
      });
    });
    host.querySelector('[data-undo-auto]')?.addEventListener('click',async event => {
      const button = event.currentTarget;button.disabled = true;
      try {
        const result = await api(`${base(job.id)}/undo-auto`,{method:'POST',body:{publication_id:item.id}});
        toast(result.warning || '已撤销自动登记，这个帖子不会再自动登记到这条时间线。',Boolean(result.warning));
        if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});
      } catch (problem) {button.disabled = false;toast(problem.message || '没能撤销自动登记。',true);}
    });
    host.querySelectorAll('[data-candidate-dismiss]').forEach(button => button.addEventListener('click',async () => {
      button.disabled = true;
      try {await api(`${base(job.id)}/dismiss`,{method:'POST',body:{post_id:button.dataset.candidateDismiss,timeline_uid:data.timeline_uid}});await mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});}
      catch (problem) {button.disabled = false;toast(problem.message || '没能忽略这条帖子。',true);}
    }));
    const verify = host.querySelector('[data-verify-video]');
    if (verify) verify.addEventListener('click',async () => {
      verify.disabled = true;
      const message = host.querySelector('[data-verification]');message.textContent = '正在核对视频内容…';
      try {const result = await api(`${base(job.id)}/verify`,{method:'POST',body:{publication_id:item.id}});message.textContent = result.matches ? '视频内容与登记时一致。' : '视频内容与登记时不一致，请从备份恢复。';}
      catch (error) {message.textContent = error.message || '核对暂未完成。';}
      finally {verify.disabled = false;}
    });
    const form = host.querySelector('.video-publication-form');
    // Each timeline keeps its own calendar mark and day. The newest keeps the order's own entry, which the content list reads.
    const timelineKey = multi && data.timeline_uid !== lines[0].unique_id ? String(data.timeline_uid || '').replace(/[^A-Za-z0-9-]/g,'').slice(0,120) : '';
    if (!item) void bindCalendarPlan(host,`video:${job.id}${timelineKey ? `_tl_${timelineKey}` : ''}`,api,{form,after:onSaved});
    if (!form) return;
    const lockUncertain = () => {
      const uncertain = savedRequest && savedRequest.uncertain !== false;
      form.querySelectorAll('input[name],textarea,select,[data-refresh-publication]').forEach(node => node.disabled = Boolean(uncertain));
      if (uncertain) {
        form.elements.file_confirmed.checked = true;
        form.querySelector('[type="submit"]').textContent = '重试同一登记';
        form.querySelector('[data-save-status]').textContent = '上次结果尚未确认，输入已保留。请重试同一登记，完成后再修改。';
      }
    };
    lockUncertain();
    void assistIdentity(form,api);
    // 登记 on a detected post fills the form as a pasted link would, then saves through the same path.
    host.querySelectorAll('[data-candidate-use]').forEach(button => button.addEventListener('click',() => {
      const post = (data.candidates || []).find(item => item.post_id === button.dataset.candidateUse);
      if (!post || form.dataset.busy || form.elements.post_id.disabled) return;
      form.elements.post_id.value = post.post_id;form.elements.published_on.value = post.published_on;
      if (post.account) {form.elements.account.value = post.account;form.elements.post_url.value = tiktokUrl({post_id:post.post_id,account:post.account});}
      if (form.elements.calendar_uploaded) form.elements.calendar_uploaded.value = '1';
      void form._markUploaded?.();
      form.requestSubmit();
    }));
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
      const body = {...values,revision:data.revision,source_signature:data.source_signature,file_confirmed:form.elements.file_confirmed.checked,calendar_uploaded:values.calendar_uploaded === '1',...(multi ? {timeline_uid:data.timeline_uid} : {})};
      // A saved request remains exactly reproducible after a lost response or page reload.
      const old = savedRequest?.body;
      const sameInput = old && ['file_path','account','post_id','published_on','post_url','note','calendar_uploaded','timeline_uid'].every(key => String(old[key] || '') === String(body[key] || ''));
      const request = (savedRequest && savedRequest.uncertain !== false) || sameInput ? old : pending(key,body);
      savedRequest = requests.get(key) || savedRequest;
      savedRequest.uncertain = true;remember(key,savedRequest);
      lockUncertain();
      form.dataset.busy = '1';error.hidden = true;
      form.querySelectorAll('button').forEach(button => button.disabled = true);
      form.querySelector('[data-save-status]').textContent = '正在保存发布登记…';
      try {await api(base(job.id),{method:'POST',body:request});clearPending(key);if (multi) host._videoPublicationOnRegistered?.(data.timeline_uid,lines);if (onSaved) await onSaved();else await mount(host,{job,api,onSaved,onLoaded,onTimelineChanged});}
      catch (problem) {
        const definitive = Number.isInteger(problem.status) && problem.status >= 400 && problem.status < 500;
        savedRequest.uncertain = !definitive;remember(key,savedRequest);
        error.textContent = (problem.message || '结果尚未确认；重试会恢复同一登记。') + (definitive ? ' 可读取最新版本，核对现有登记后再修改。' : ' 输入已保留，请重试同一登记。');
        error.hidden = false;form.querySelector('[data-save-status]').textContent = '';form.querySelector('[data-refresh-publication]').hidden = !definitive;
      }
      finally {delete form.dataset.busy;form.querySelectorAll('button').forEach(button => button.disabled = false);lockUncertain();}
    });
  }
  // Shows another timeline's registration; a no-op when it is already shown or being read.
  function select(host,uid) {
    if (!host?._videoPublicationArgs || !uid || (host._videoPublicationTimeline || host._videoPublicationShown) === uid) return;
    host._videoPublicationTimeline = uid;
    return mount(host,host._videoPublicationArgs);
  }
  // A finished Notion upload marks its timeline at once, without reloading the panel (which would drop a typed post ID).
  function markNotionUploaded(host,uid) {
    const lines = host?._videoPublicationLines;if (!lines) return;
    const line = lines.find(row => row.unique_id === uid);if (!line) return;
    line.notion_uploaded = true;host._videoPublicationOnLines?.(lines,host._videoPublicationShown);
  }
  return {mount,normalize,localDate,postedOn,followPostDate,copyBlock,bindCopy,calendarLine,calendarChoice,calendarPlanMarkup,bindCalendarPlan,registrationTag,registrationHint,tiktokUrl,select,markNotionUploaded};
})();
