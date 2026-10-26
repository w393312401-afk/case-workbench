/* Saved review lessons retain their source and uncertainty when reused. */
window.ReviewExperiences = (() => {
  const kinds = {success:'成功做法',failure:'失败教训'};
  const confidence = {insufficient:'证据不足',preliminary:'初步判断',supported:'复测支持'};
  const referenceValues = {none:'无参考价值 · 仅留档',weak:'弱参考价值',medium:'中等参考价值',high:'高参考价值'};
  const fields = ['product','kind','dimension','practice','conditions','evidence_note','confidence','scope','direction_id','status'];
  const drafts = new Map(), pending = new Map();
  const enabled = () => !window.PublicTextMode && !window.WorkbenchGuest && !window.GuestMode;
  const context = () => window.WorkbenchShell?.product?.() ?? null;
  const safe = value => esc(value ?? '');
  const editable = item => Object.fromEntries(fields.map(key => [key,item[key] ?? '']));
  const options = (values,selected,empty='') => (empty ? `<option value="">${safe(empty)}</option>` : '') + Object.entries(values).map(([value,label]) => `<option value="${value}"${selected === value ? ' selected' : ''}>${safe(label)}</option>`).join('');
  const sourceURL = source => {
    const hash = `#videos?video=${encodeURIComponent(source.video_id || '')}&feedback_window=${encodeURIComponent(source.window_end || '')}`;
    return window.WorkbenchShell?.url?.(hash,source.product) || hash;
  };
  const sourceKey = source => [source.product,source.video_id,source.window_end,source.reviewed_at].join('|');
  const notice = (node,text) => {node.textContent = text;node.hidden = !text;};
  function sourceBlock(item) {
    const source = item.source || {}, review = source.review || {}, video = source.video || {};
    const metrics = video.metrics || {}, labels = {views:'播放',orders:'订单',gmv:'成交额',product_clicks:'商品点击',window_views:'窗口播放'};
    const figures = Object.entries(labels).filter(([key]) => metrics[key] !== null && metrics[key] !== undefined).map(([key,label]) => `${label} ${metrics[key]}`).join(' · ');
    return `<div class="experience-source"><a data-experience-source href="${safe(sourceURL(review))}">来源复盘 · ${safe(video.headline || video.title || review.video_id || '查看视频')}</a><p>${safe([review.product,video.account,video.posted_on && `发布 ${video.posted_on}`,review.window_end && `观察窗到 ${review.window_end}`].filter(Boolean).join(' · '))}</p>${review.reviewed_at ? `<p>复盘确认 ${safe(review.reviewed_at)}</p>` : ''}${source.publications?.length ? `<p>绑定稿件 ${source.publications.map(row => `${safe(row.script_id)}${row.script_version ? ` · v${safe(row.script_version)}` : ''}`).join('；')}</p>` : ''}${video.captured_at || figures ? `<details><summary>提炼时的数据快照${video.captured_at ? ` · ${safe(video.captured_at)}` : ''}</summary><p>${safe(figures || '暂无可用数据')}</p>${video.last_data ? `<p>数据截至 ${safe(video.last_data)}</p>` : ''}<p>此处保留提炼经验时取得的数据，供核对证据。</p></details>` : ''}${review.note ? `<details><summary>原复盘结论</summary><blockquote>${safe(review.note)}</blockquote></details>` : ''}</div>`;
  }
  function historyBlock(item) {
    if (!item.history?.length) return '';
    return `<details class="experience-history"><summary>修改记录 · ${item.history.length}</summary>${item.history.map(entry => {
      const row = entry.snapshot || entry.experience || entry;
      return `<div><strong>v${safe(row.version || entry.version || '—')} · ${safe(row.status === 'retired' ? '已停用' : '启用')}</strong><small>${safe(entry.at || entry.updated_at || row.updated_at || '')}</small><p>${safe(row.practice || '')}</p>${row.conditions ? `<p>适用条件：${safe(row.conditions)}</p>` : ''}${row.evidence_note ? `<p>证据：${safe(row.evidence_note)}</p>` : ''}</div>`;
    }).join('')}</details>`;
  }
  function card(item) {
    return `<article class="experience-card" data-experience-id="${safe(item.id)}"><div class="experience-card-head"><div class="experience-tags"><span class="experience-tag is-${item.kind === 'success' ? 'success' : 'failure'}">${safe(kinds[item.kind] || item.kind)}</span>${referenceValues[item.reference_value] ? `<span class="experience-tag">${safe(referenceValues[item.reference_value])}</span>` : ''}${item.status === 'retired' ? '<span class="experience-tag">已停用</span>' : ''}${item.source_status && item.source_status !== 'current' ? '<span class="experience-tag is-warning">来源待核对，暂不自动带入</span>' : ''}</div><button type="button" class="subtle compact" data-experience-edit="${safe(item.id)}">编辑</button></div><h3>${safe(item.practice)}</h3><details class="experience-evidence"><summary>依据与管理</summary><dl><dt>适用条件</dt><dd>${safe(item.conditions || '未补充，使用前需核对场景。')}</dd><dt>补充证据</dt><dd>${safe(item.evidence_note || '未补充，见来源复盘。')}</dd></dl><p class="experience-scope">${safe(confidence[item.confidence] || '证据不足')} · ${item.scope === 'direction' ? `方向 ${safe(item.direction_id)}` : safe(item.product)} · v${safe(item.version)}</p>${sourceBlock(item)}${historyBlock(item)}<div class="experience-card-actions"><button type="button" class="subtle compact" data-experience-status="${safe(item.id)}">${item.status === 'retired' ? '恢复使用' : '停用'}</button></div></details></article>`;
  }
  function modal(className,title) {
    const dialog = document.createElement('dialog');
    dialog.className = className;
    dialog.setAttribute('aria-label',title);
    dialog.addEventListener('close',() => dialog.remove(),{once:true});
    document.body.append(dialog);
    return dialog;
  }
  function open({product=context() || '',q='',kind='',status='active',direction_id=''} = {}) {
    if (!enabled()) return;
    const previous = document.querySelector('.experience-library-dialog');
    if (previous) previous.close();
    const initialContext = context(), dialog = modal('experience-library-dialog','复盘经验');
    let sequence = 0, revision = null, items = [], timer, busy = false, currentStatus = status;
    const current = seq => dialog.isConnected && seq === sequence && context() === initialContext && enabled();
    dialog.innerHTML = `<div class="dialog-heading"><div><h2>复盘经验</h2><p>写稿、审稿时自动参考</p></div><button type="button" class="subtle" data-close>关闭</button></div><div class="experience-library-body"><form class="experience-filters" role="search"><label><span>搜索经验</span><input name="q" type="search" placeholder="搜索做法、条件或来源" value="${safe(q)}"></label><label><span>类型</span><select name="kind">${options(kinds,kind,'全部类型')}</select></label></form><div class="experience-list-heading"><p data-experience-count role="status">正在读取…</p><button type="button" class="subtle compact" data-experience-archive></button></div><p class="experience-error" role="alert" data-experience-error hidden></p><button type="button" class="subtle compact" data-experience-refresh hidden>重试</button><div class="experience-list" aria-live="polite"></div></div>`;
    const list = dialog.querySelector('.experience-list'), error = dialog.querySelector('[data-experience-error]'), filters = dialog.querySelector('form');
    const load = async () => {
      const seq = ++sequence;
      if (!current(seq)) {notice(error,'产品已切换，请关闭后从当前产品重新打开经验库。');return;}
      const query = new URLSearchParams({product,status:currentStatus,...(direction_id ? {direction_id} : {}),...Object.fromEntries(new FormData(filters))});
      dialog.querySelector('[data-experience-archive]').textContent = currentStatus === 'retired' ? '返回使用中' : '查看已停用';
      dialog.querySelector('[data-experience-refresh]').hidden = true;
      notice(error,'');dialog.querySelector('[data-experience-count]').textContent = '正在读取…';
      try {
        const data = await api('/api/review-experiences?' + query);
        if (!current(seq)) return;
        revision = data.revision;items = data.items || [];
        dialog.querySelector('[data-experience-count]').textContent = `${data.total ?? items.length} 条${currentStatus === 'retired' ? '已停用' : ''}经验`;
        list.innerHTML = items.length ? items.map(card).join('') : '<div class="experience-empty"><strong>暂无经验</strong><p>在已确认的复盘中点“记一条经验”。</p></div>';
        list.querySelectorAll('[data-experience-edit]').forEach(button => button.onclick = () => {
          if (!current(seq) || busy) return;
          editor({item:items.find(row => row.id === button.dataset.experienceEdit),onSaved:load});
        });
        list.querySelectorAll('[data-experience-status]').forEach(button => {
          if (pending.has('status:' + button.dataset.experienceStatus)) button.textContent = '核对上次操作';
          button.onclick = async () => {
          if (!current(seq) || busy) return;
          const item = items.find(row => row.id === button.dataset.experienceStatus);
          const key = 'status:' + item.id;
          if (!pending.has(key)) pending.set(key,{request_id:crypto.randomUUID(),revision,experience:{id:item.id,product:item.product,status:item.status === 'retired' ? 'active' : 'retired'}});
          busy = true;button.disabled = true;notice(error,'');
          try {
            const result = await api('/api/review-experiences',{method:'POST',body:pending.get(key)});
            if (!result.item?.id) throw new Error('未收到保存结果。');
            pending.delete(key);
            if (!current(seq)) return;
            toast(result.item.status === 'retired' ? '经验已停用，后续任务不再自动带入。' : '经验已恢复使用。');
            await load();
          } catch (failure) {
            if (failure.status && failure.status < 500) pending.delete(key);
            if (!current(seq)) return;
            if (failure.status === 409) {await load();notice(error,'经验已在其他页面更新，已读取最新内容；请核对后再操作。');}
            else {notice(error,(failure.message || '暂时无法保存。') + (pending.has(key) ? ' 结果尚未确认，再次点击会核对同一请求。' : ''));if (pending.has(key)) button.textContent = '核对上次操作';}
          } finally {busy = false;if (button.isConnected) button.disabled = false;}
          };
        });
        list.querySelectorAll('[data-experience-source]').forEach(link => link.onclick = () => dialog.close());
      } catch (failure) {
        if (!current(seq)) return;
        dialog.querySelector('[data-experience-count]').textContent = '暂未读到经验';
        dialog.querySelector('[data-experience-refresh]').hidden = false;
        list.innerHTML = '';notice(error,failure.message || '经验暂时无法读取，请重试。');
      }
    };
    filters.onsubmit = event => {event.preventDefault();clearTimeout(timer);void load();};
    filters.oninput = event => {clearTimeout(timer);timer = setTimeout(load,event.target.tagName === 'SELECT' ? 0 : 220);};
    dialog.querySelector('[data-experience-refresh]').onclick = load;
    dialog.querySelector('[data-experience-archive]').onclick = () => {if (busy) return;currentStatus = currentStatus === 'retired' ? 'active' : 'retired';void load();};
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close',() => {sequence++;clearTimeout(timer);});
    dialog.showModal();void load();return dialog;
  }
  function fromReview(data,{onSaved} = {}) {
    if (!enabled()) return;
    const source = data?.source_review;
    if (data?.review?.state !== 'reviewed' || !source || !source.product || !source.video_id || !source.window_end || !source.reviewed_at) {
      toast('请先确认保存本条复盘，再提炼经验。',true);return;
    }
    return editor({data,onSaved});
  }
  function editor({item,data,onSaved}) {
    if (!enabled() || document.querySelector('.experience-form-dialog[open]')) return;
    const source = item?.source?.review || structuredClone(data.source_review), product = item?.product || data.product;
    const key = item ? 'edit:' + item.id : 'new:' + sourceKey(source), initialContext = context();
    const selectedDirection = data?.summary?.direction_id || '';
    const defaults = item ? editable(item) : {product,kind:{positive:'success',negative:'failure'}[data.review.outcome] || '',dimension:'',practice:data.review.note || source.note || '',conditions:'',evidence_note:'',confidence:confidence[data.review.evidence] ? data.review.evidence : 'insufficient',scope:selectedDirection ? 'direction' : 'product',direction_id:selectedDirection,status:'active'};
    const values = pending.get(key)?.experience || drafts.get(key) || defaults;
    let metadata = editable(values);
    const dialog = modal('experience-form-dialog',item ? '编辑经验' : '记一条经验');
    let revision = null, expectedVersion = item?.version, busy = false, ready = false, conflictedItem = null, readSequence = 0;
    const current = () => dialog.isConnected && enabled() && context() === initialContext;
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>${item ? '编辑经验' : '记一条经验'}</h2><button type="button" class="subtle" data-close>关闭</button></div><div class="experience-form-body"><fieldset class="experience-kind"><legend>经验类型</legend>${Object.entries(kinds).map(([key,label]) => `<label><input type="radio" name="kind" value="${key}"${values.kind === key ? ' checked' : ''} required><span>${label}</span></label>`).join('')}</fieldset><label class="field"><span>下次记住什么</span><textarea name="practice" rows="3" maxlength="3000" required placeholder="一句话写清值得重复或需要避免的做法。">${safe(values.practice)}</textarea></label><details class="experience-extra"><summary>补充条件与证据（可选）</summary><label class="field"><span>适用条件</span><textarea name="conditions" rows="2" maxlength="3000" placeholder="什么情况下适用，哪些情况不适用？">${safe(values.conditions)}</textarea></label><label class="field"><span>补充证据</span><textarea name="evidence_note" rows="2" maxlength="4000" placeholder="还需要记下哪些观察或限制？">${safe(values.evidence_note)}</textarea></label><div class="experience-source-note"><strong>来源复盘</strong><blockquote>${safe(source.note)}</blockquote><p>视频 ${safe(source.video_id)} · 观察窗到 ${safe(source.window_end)}</p><p>${safe(confidence[values.confidence] || '证据不足')} · ${values.scope === 'direction' ? `方向 ${safe(values.direction_id)}` : safe(product)}</p></div></details><p class="experience-intro">来源与数据自动保留，${values.scope === 'direction' ? '同方向' : '同产品'}写稿、审稿时参考。</p><div class="experience-conflict" data-conflict hidden></div><p class="experience-error" role="alert" data-error hidden></p><p class="experience-form-status" role="status" data-load-status>正在读取…</p></div><div class="dialog-footer"><button type="button" class="subtle" data-reload hidden>重试</button><button type="submit" class="primary">保存</button></div></form>`;
    const form = dialog.querySelector('form'), error = form.querySelector('[data-error]'), loadStatus = form.querySelector('[data-load-status]'), conflict = form.querySelector('[data-conflict]');
    const collect = () => ({...metadata,product,...Object.fromEntries(new FormData(form))});
    const remember = () => {if (!pending.has(key)) drafts.set(key,collect());};
    const lock = () => {
      const frozen = busy || pending.has(key);
      form.querySelectorAll('input,textarea,select').forEach(field => field.disabled = frozen);
      form.querySelector('[type=submit]').disabled = busy || !ready || !!conflictedItem || !current();
      form.querySelector('[type=submit]').textContent = pending.has(key) ? '重试保存' : '保存';
      form.querySelector('[data-reload]').disabled = busy || pending.has(key);
    };
    const load = async () => {
      if (!current()) {ready = false;notice(error,'产品已切换，填写内容已保留；请回到原产品后继续。');lock();return;}
      const seq = ++readSequence;ready = false;lock();notice(loadStatus,'正在读取…');form.querySelector('[data-reload]').hidden = true;
      try {
        const result = await api('/api/review-experiences?' + new URLSearchParams({product,status:'all'}));
        if (!current() || seq !== readSequence) return;
        revision = result.revision;
        if (!revision) throw new Error('未取得保存版本，请重新读取。');
        if (item) {
          const latest = (result.items || []).find(row => row.id === item.id);
          if (!latest) throw new Error('这条经验已不存在。填写内容已保留，请返回经验库核对。');
          if (latest.version !== expectedVersion) {
            conflictedItem = latest;conflict.hidden = false;
            conflict.innerHTML = `<strong>其他页面已修改这条经验</strong><p>最新做法：${safe(latest.practice)}</p><p>适用条件：${safe(latest.conditions)}</p><p>证据：${safe(latest.evidence_note)}</p><p>你的填写保留在上方，核对后再决定是否保存。</p><button type="button" class="secondary compact" data-accept-conflict>已核对，保留我的填写继续编辑</button>`;
            conflict.querySelector('button').onclick = () => {expectedVersion = conflictedItem.version;for (const key of ['dimension','confidence','scope','direction_id','status']) metadata[key] = conflictedItem[key];conflictedItem = null;conflict.hidden = true;notice(error,'');lock();};
          }
        }
        ready = true;notice(loadStatus,'');
      } catch (failure) {if (current() && seq === readSequence) {notice(loadStatus,'');notice(error,failure.message || '暂时无法读取，请重试。');form.querySelector('[data-reload]').hidden = false;}}
      finally {if (current() && seq === readSequence) lock();}
    };
    form.oninput = remember;
    form.querySelector('[data-reload]').onclick = load;
    form.querySelector('[data-close]').onclick = () => {if (!busy) {remember();dialog.close();}};
    dialog.addEventListener('cancel',event => {if (busy) event.preventDefault();else remember();});
    dialog.addEventListener('close',() => {readSequence++;});
    form.onsubmit = async event => {
      event.preventDefault();if (busy || conflictedItem || !ready) return;
      if (!current()) {remember();notice(error,'产品已切换，填写内容已保留；请回到原产品后继续。');lock();return;}
      if (!pending.has(key)) {
        const value = collect();
        for (const field of ['practice','conditions','evidence_note']) {
          value[field] = String(value[field] || '').trim();
          if (field === 'practice' && !value[field]) {notice(error,'写一句下次要记住的做法。');form.elements[field].focus();return;}
        }
        value.dimension = value.dimension.trim();value.direction_id = value.scope === 'direction' ? value.direction_id.trim() : '';
        drafts.set(key,value);
        pending.set(key,{request_id:crypto.randomUUID(),revision,experience:{...editable(value),...(item ? {id:item.id} : {})},...(!item ? {source_review:source} : {})});
      }
      busy = true;notice(error,'');lock();
      try {
        const result = await api('/api/review-experiences',{method:'POST',body:pending.get(key)});
        if (!result.item?.id || !result.revision) throw new Error('未收到完整保存结果。');
        pending.delete(key);drafts.delete(key);
        const activeContext = current();dialog.close();toast(item ? '经验修改已保存。' : '经验已保存，下一轮会按适用范围带入。');
        if (activeContext) {try {await onSaved?.(result);} catch (_) {toast('经验已保存，列表暂未刷新，请重新读取。',true);}}
      } catch (failure) {
        if (failure.status && failure.status < 500) pending.delete(key);
        if (!dialog.isConnected) return;
        if (failure.status === 409) {
          notice(error,(failure.message || '经验或来源已变化。') + ' 填写内容已保留。正在读取最新版本，请核对后再保存；若来源复盘已改，请从最新复盘重新沉淀。');
          busy = false;await load();
        } else notice(error,(failure.message || '保存暂未完成。') + (pending.has(key) ? ' 结果尚未确认，重试会核对同一请求，避免重复保存。' : ''));
      } finally {busy = false;if (dialog.isConnected) lock();}
    };
    dialog.showModal();
    if (pending.has(key)) {revision = pending.get(key).revision;ready = true;notice(loadStatus,'');notice(error,'上次保存结果尚未确认，重试将核对同一请求。');lock();}
    else {lock();void load();}
    return dialog;
  }
  return {open,fromReview};
})();
