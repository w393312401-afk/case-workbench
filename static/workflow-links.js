'use strict';

// Read the existing provenance graph; opening an entry never creates another task.
window.WorkbenchLinks = (() => {
  const names = {draft:'待制作',voiced:'已配音',timeline:'已建时间线',editing:'剪辑中',awaiting_review:'待人工审查',approved:'已确认',needs_material:'待补素材',needs_verification:'素材待核实',needs_repair:'待修正',verification_failed:'核验未完成',published:'已登记发布',picking:'选图中',picked:'底图已选齐',reviewing:'成图待核对',failed:'失败',cancelled:'已停止',interrupted:'已中断',running:'进行中',completed:'已完成',adopted:'已采纳'};
  const idOf = item => item?.script_id || item?.id || '';
  const pageURL = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const inProduct = (item,product) => !item.product || (window.WorkbenchShell?.normalizeProduct?.(item.product) || item.product) === (window.WorkbenchShell?.normalizeProduct?.(product) || product);
  function productionURL(item,kind,product) {
    let step = '';
    if (kind === 'carousel') step = item.publication?.post_id || item.status === 'approved' || item.status === 'published' ? 'delivery' : item.slides?.some(slide => slide.outputs?.length) ? 'images' : '';
    else step = item.current_publication?.post_id ? 'publish' : item.edits?.length ? 'edit' : item.timelines?.length ? 'timeline' : item.takes?.length ? 'voice' : '';
    return pageURL(`#production/${encodeURIComponent(item.id)}${step ? '?step='+step : ''}`,item.product || product);
  }
  function mount({kind,record,script = null}) {
    const heading = document.querySelector('#content .page-heading');
    if (!heading) return;
    const host = document.createElement('section');host.id = 'workflow-links';host.className = 'section-card workflow-links';host.setAttribute('aria-label','关联任务与输出');
    // The adopted-copy page is a secondary version detail. Read the text first;
    // keep all provenance in this one section below it.
    const detail = kind === 'script' && document.querySelector('#content .script-detail-grid');
    (detail || heading).after(host);
    const ownerPath = location.hash.split('?')[0], product = record.product || record.context?.product || script?.product || window.WorkbenchShell?.product() || '';
    let sequence = 0;
    const current = seq => host.isConnected && ownerPath === location.hash.split('?')[0] && sequence === seq;
    // Orders and version details keep provenance behind a single secondary fold.
    const fold = true;
    const foldKey = `workflow-links:${kind}:open`;
    const foldOpen = () => {try {return localStorage.getItem(foldKey) === '1';} catch {return false;}};
    const kindNames = {direction:'方向',variant:'变体',case:'案例',review:'复盘',plan:'计划',task:'写稿',script:'采用稿',production:'视频',video:'视频',carousel:'图文',table:'逐张表',publication:'发布'};
    const show = (groups,notes = [],busy = false) => {
      const retry = `<button type="button" class="subtle compact" data-workflow-retry ${busy ? 'disabled' : ''}>${busy ? '读取中…' : notes.length ? '重新读取关联' : '刷新关联'}</button>`;
      const attention = notes.filter(note => /无法|不一致|不可读|多个变体|未核对到唯一/.test(note));
      // Variant attribution waits on the user, not on a fault: its reason rides on the fold title, the full note inside it.
      const variant = attention.filter(note => /多个变体|未核对到唯一/.test(note)), alerts = attention.filter(note => !variant.includes(note));
      const reason = variant.some(note => /多个变体/.test(note)) ? '对应多个变体' : variant.length ? '未核对到唯一执行变体' : '';
      const body = `${fold ? '' : `<div class="section-title-row"><h2>关联任务与输出</h2>${retry}</div>`}${groups.map(group => `<div class="workflow-group"><h3>${esc(group.title)}</h3><div class="workflow-link-grid">${group.links.map(link => `<a class="workflow-link" data-workflow-kind="${esc(link.kind)}" href="${esc(link.href)}"><strong>${esc(link.title)}</strong>${link.note ? `<small>${esc(link.note)}</small>` : ''}</a>`).join('')}</div></div>`).join('')}${notes.filter(note => !attention.includes(note)).map(note => `<p class="context-note workflow-note" role="status">${esc(note)}</p>`).join('')}${busy && !groups.length ? '<p class="context-note" role="status">正在核对来源与关联记录…</p>' : ''}${fold ? `<div class="workflow-fold-tools"><button type="button" class="subtle compact" data-workflow-copy>复制关联信息</button>${retry}</div>` : ''}`;
      if (!fold) host.innerHTML = body;
      else {
        const kinds = [...new Set(groups.flatMap(group => group.links.map(link => link.kind)))], total = groups.reduce((sum,group) => sum + group.links.length,0);
        host.innerHTML = `${alerts.map(note => `<p class="workflow-alert" role="status">${esc(note)}</p>`).join('')}<details class="workflow-fold" ${foldOpen() ? 'open' : ''}><summary><span class="workflow-fold-title">关联任务与输出</span><span class="workflow-fold-kinds">${kinds.map(item => `<span class="workflow-kind" data-workflow-kind="${esc(item)}">${esc(kindNames[item] || '关联')}</span>`).join('')}</span><small>${busy && !groups.length ? '读取中…' : `${total} 项${reason ? ` · <span class="workflow-fold-attention">需核对：${esc(reason)}</span>` : alerts.length ? ' · 需核对' : ''}`}</small></summary>${variant.map(note => `<p class="workflow-alert workflow-fold-alert" role="status">${esc(note)}</p>`).join('')}${body}</details>`;
        host.classList.add('workflow-links-folded');
        host.querySelector('details').addEventListener('toggle',event => {try {localStorage.setItem(foldKey,event.currentTarget.open ? '1' : '0');} catch {}});
      }
      host.querySelector('[data-workflow-retry]').onclick = () => void load();
      host.querySelector('[data-workflow-copy]')?.addEventListener('click',() => copyText(groups.flatMap(group => group.links.map(link => `${group.title} · ${link.title}\n${link.note || ''}\n${link.href}`)).join('\n\n'),'关联信息已复制'));
    };
    async function load() {
      const seq = ++sequence, groups = [], notes = [], seen = new Set();show([],[],true);
      const add = (groupTitle,link) => {
        const key = `${link.kind}:${link.href}`;if (seen.has(key)) return;seen.add(key);
        let group = groups.find(item => item.title === groupTitle);if (!group) {group = {title:groupTitle,links:[]};groups.push(group);}group.links.push(link);
      };
      let fullScript = script;
      if (kind === 'video' || kind === 'carousel') {
        try {const result = await api(`/api/scripts/${encodeURIComponent(record.script_id)}`);fullScript = result.script;if (!fullScript || idOf(fullScript) !== record.script_id || !inProduct(fullScript,product)) throw new Error('采用稿来源不一致');}
        catch (error) {fullScript = null;notes.push(error.status === 404 ? '来源采用稿已不可读；制作单中保留的内容仍可查看。' : `来源采用稿暂时无法读取：${error.message || '请重试'}`);}
        if (!current(seq)) return;
      }
      const source = fullScript?.source || record.source || {};
      const creationContext = source.snapshot?.creation_context || {};
      const sourceReview = record.context?.source_review || record.context?.plan?.source_review || creationContext.source_review || creationContext.plan?.source_review;
      if (sourceReview?.video_id && sourceReview.product && inProduct(sourceReview,product)) add('来源与版本',{kind:'review',title:'来源复盘与下一轮',note:[sourceReview.video_id,sourceReview.window_end,sourceReview.reviewed_at ? '保留发起时结论' : ''].filter(Boolean).join(' · '),href:window.FeedbackChain?.sourceURL(sourceReview) || pageURL(`#videos?video=${encodeURIComponent(sourceReview.video_id)}&feedback_window=${encodeURIComponent(sourceReview.window_end || '')}`,sourceReview.product)});
      // Adopted text and its version-detail link live with the inline copy/history,
      // rather than adding another stop beside the writing output.
      if (source.task_id && source.task_id !== record.task_id) add('来源与版本',{kind:'task',title:'查看完整写稿输出',note:source.round_id ? '直接查看采纳时的那一轮' : '来源 AI 任务',href:pageURL(`#ai/${encodeURIComponent(source.task_id)}${source.round_id ? '?round='+encodeURIComponent(source.round_id) : ''}`,product)});
      const caseId = fullScript?.case_id || record.case_id;
      if (caseId) add('来源与版本',{kind:'case',title:'关联案例',note:caseId,href:pageURL(`#case/${encodeURIComponent(caseId)}`,product)});
      if (source.case_id && source.case_id !== caseId) add('来源与版本',{kind:'case',title:'写稿参考案例',note:source.case_id,href:pageURL(`#case/${encodeURIComponent(source.case_id)}`,product)});
      let direction = record.direction_id || record.variant_link?.direction_id || fullScript?.direction_id || fullScript?.variant_link?.direction_id || record.context?.direction_id;
      if (!direction && !window.PublicTextMode && (kind === 'video' || kind === 'carousel')) {
        try {
          const association = await api('/api/variant-links/source?'+new URLSearchParams({kind:'production',id:record.id,product}));
          if (association.record?.id === record.id && inProduct(association.record,product) && association.existing_link?.record_id === record.id && inProduct(association.existing_link,product)) direction = association.existing_link.direction_id;
        } catch (error) {notes.push(`执行变体关联暂时无法读取：${error.message || '请重试'}`);}
        if (!current(seq)) return;
      }
      const requests = [];
      const read = (key,path,validate) => requests.push({key,promise:api(path).then(value => {if (validate && !validate(value)) throw new Error('关联记录返回不完整');return value;})});
      if (!window.PublicTextMode) {
        read('production',`/api/production?product=${encodeURIComponent(product)}`,data => Array.isArray(data.jobs) && Array.isArray(data.carousels));
        if (direction) read('progress',`/api/direction-progress/${encodeURIComponent(direction)}?product=${encodeURIComponent(product)}`,data => data.direction_id === direction && inProduct(data,product) && Array.isArray(data.variants));
        if (fullScript && kind !== 'video') read('tables',`/api/production/carousel-tables?script_id=${encodeURIComponent(idOf(fullScript))}`,data => Array.isArray(data.tables));
      }
      if (kind === 'task') read('creation',`/api/creation?product=${encodeURIComponent(product)}`,data => Array.isArray(data.scripts));
      const results = await Promise.allSettled(requests.map(item => item.promise));if (!current(seq)) return;
      const loaded = {};
      results.forEach((result,index) => {
        const key = requests[index].key;
        if (result.status === 'fulfilled') loaded[key] = result.value;
        else notes.push(`${{production:'制作单',progress:'执行变体',tables:'图文任务',creation:'采用稿'}[key]}关联暂时无法读取：${result.reason?.message || '请重试'}`);
      });
      const scripts = kind === 'task' ? (loaded.creation?.scripts || []).filter(item => item.source?.task_id === record.task_id && inProduct(item,product)) : fullScript ? [fullScript] : [];
      // Writing pages already show saved copy and versions in #ai-task-adopted.
      const scriptIds = new Set(scripts.map(idOf));
      if ((kind === 'video' || kind === 'carousel') && record.script_id) scriptIds.add(record.script_id);
      const productions = [...(loaded.production?.jobs || []).map(item => ({...item,kind:'video'})),...(loaded.production?.carousels || []).map(item => ({...item,kind:'carousel'}))].filter(item => inProduct(item,product));
      const addProduction = (item,group) => {
        if (item.id === record.id) return;
        const publication = item.kind === 'video' ? item.current_publication : item.publication;
        add(group,{kind:item.kind,title:`${item.kind === 'carousel' ? '图文' : '视频'} · ${item.title || item.id}`,note:[item.id,names[item.status] || '查看进度',item.archived || item.status === 'archived' ? '已归档' : '',publication?.post_id ? `已登记发布 ${publication.post_id}` : '尚未登记发布',item.kind === 'carousel' && item.slides?.some(slide => slide.outputs?.length) ? '查看成图' : item.kind === 'video' && item.edits?.length ? '查看剪辑输出' : ''].filter(Boolean).join(' · '),href:productionURL(item,item.kind,product)});
      };
      productions.filter(item => scriptIds.has(item.script_id)).forEach(item => addProduction(item,'本版制作与输出'));
      for (const table of loaded.tables?.tables || []) {
        if (table.script_id && !scriptIds.has(table.script_id)) continue;
        if (!table.id || (table.status !== 'running' && table.has_result !== true)) continue;
        // Existing readable orders are listed above. A receipt by itself is not
        // proof the order still exists; keep the saved table reachable instead.
        if ((kind === 'carousel' && table.carousel_id === record.id) || (table.carousel_id && productions.some(item => item.kind === 'carousel' && item.id === table.carousel_id && scriptIds.has(item.script_id)))) continue;
        const state = table.status === 'completed' ? '待核对' : names[table.status] || '待核对';
        const note = [Number.isInteger(table.slide_count) && table.slide_count > 0 ? `${table.slide_count} 张` : '',state,String(table.finished_at || table.started_at || '').slice(0,10),table.carousel_missing ? '关联制作单缺失' : '',table.has_result === true && table.status !== 'completed' ? '原表已保留' : ''].filter(Boolean).join(' · ');
        add('图文逐张表',{kind:'table',title:table.has_result === true ? '查看原逐张表' : '查看逐张表进度',note,href:pageURL(`#content/${encodeURIComponent(idOf(fullScript))}?table=${encodeURIComponent(table.id)}`,product)});
      }
      const variants = (loaded.progress?.variants || []).filter(variant => {
        const records = variant.records || {};
        return kind === 'task' ? records.tasks?.some(item => item.id === record.task_id) : kind === 'script' ? records.scripts?.some(item => item.id === idOf(record)) : records.productions?.some(item => item.id === record.id);
      });
      const exactVariants = (kind === 'task' || variants.length === 1 ? variants : []).filter(variant => variant.key && Number.isInteger(variant.index) && loaded.progress.variants.filter(item => item.key === variant.key).length === 1);
      for (const variant of exactVariants) {
        const target = `#directions/${encodeURIComponent(direction)}?variant=${encodeURIComponent(variant.key)}`;
        const records = variant.records || {}, counts = [records.scripts?.length ? `${records.scripts.length} 份采用稿` : '',records.productions?.length ? `${records.productions.length} 张制作单` : '',records.publications?.length ? `${records.publications.length} 条发布` : ''].filter(Boolean);
        add('执行变体',{kind:'variant',title:`执行变体 ${variant.index + 1} · 全部输出`,note:[variant.snapshot?.progression,...counts].filter(Boolean).join(' · '),href:pageURL(target,product)});
        for (const related of records.productions || []) {
          if (scriptIds.has(related.script_id)) continue;
          const item = productions.find(item => item.id === related.id);
          if (item) addProduction(item,'同变体的其他输出');
        }
        for (const item of records.plans || []) add('测试与发布',{kind:'plan',title:item.title || '测试与安排',note:names[item.status] || '查看记录',href:pageURL(target+(item.experiment ? `&plan=${encodeURIComponent(item.id)}` : ''),product)});
        for (const item of records.publications || []) if (item.video_id) add('测试与发布',{kind:'publication',title:`发布数据 · ${item.video_id}`,note:[item.published_on,item.current === false ? '历史登记' : '',item.source_current === false ? '制作版本已变化' : ''].filter(Boolean).join(' · '),href:pageURL(`#videos?video=${encodeURIComponent(item.video_id)}`,product)});
      }
      if (direction && !exactVariants.length) {
        add('执行变体',{kind:'direction',title:'查看所属方向',note:direction,href:pageURL(`#directions/${encodeURIComponent(direction)}`,product)});
        if (loaded.progress) notes.push(variants.length > 1 ? '这条记录对应多个变体，需先核对归属。' : '尚未核对到唯一执行变体，可在方向页核对关联。');
      }
      const ownVideo = fullScript?.video_id;
      if (ownVideo) add('测试与发布',{kind:'publication',title:'已登记视频与数据',note:ownVideo,href:pageURL(`#videos?video=${encodeURIComponent(ownVideo)}`,product)});
      const ownProduction = productions.find(item => item.id === record.id);
      const ownPublication = ownProduction?.current_publication || ownProduction?.publication || record.current_publication || record.publication;
      if (ownPublication?.post_id) add('测试与发布',{kind:'publication',title:'本制作单的发布数据',note:[ownPublication.post_id,ownPublication.published_on].filter(Boolean).join(' · '),href:pageURL(`#videos?video=${encodeURIComponent(ownPublication.post_id)}`,product)});
      if (window.PublicTextMode) notes.push('制作与执行变体记录请在本机工作台查看。');
      if (!groups.length && !notes.length) notes.push(kind === 'task' ? record.action && record.action !== 'variants' ? '此任务的结论与各轮讨论保留在下方。' : scripts.length ? '已保存的采用稿及版本见下方正文，可直接开始制作。' : '还没有从此任务保存的采用稿。采纳后可在这里继续制作。' : '这条采用稿还没有关联制作任务，可从「开始制作」创建视频制作单。');
      // Put the whole execution output before the individual entries.
      groups.sort((a,b) => ['执行变体','来源与版本','采用稿','本版制作与输出','图文逐张表','同变体的其他输出','测试与发布'].indexOf(a.title) - ['执行变体','来源与版本','采用稿','本版制作与输出','图文逐张表','同变体的其他输出','测试与发布'].indexOf(b.title));
      show(groups,notes);
    }
    void load();
  }
  return {mount,productionURL};
})();
