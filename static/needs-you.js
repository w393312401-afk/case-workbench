'use strict';

// 内容进程顶部的「需要你出手」：Notion 上传失败、预览导出失败、图文步骤失败。
// 这些失败不会再出现在任务行里，不看就一直卡着；重试成功后该行自动消失。
window.NeedsYou = (() => {
  const ui = {open:false};
  const day = value => String(value || '').slice(5,16).replace('T',' ');
  const names = {notion:'Notion 上传', preview:'预览导出', carousel:'图文'};

  function paint(host,data,{url}) {
    const items = data?.items || [];
    host.hidden = !items.length;
    if (!items.length) {host.innerHTML = '';return;}
    const summary = Object.entries(data.counts || {}).map(([kind,count]) => `${names[kind] || kind} ${count}`).join(' · ');
    const rows = ui.open ? `<ul class="needs-list">${items.map(item => `<li class="needs-row"><span class="needs-kind">${esc(item.kind_label)}</span><a class="needs-main" href="${esc(url(item.hash,item.product))}" title="打开这张制作单的成片与发布"><span class="needs-title">${esc(item.timeline_name || item.job_title)}</span><span class="needs-meta">${esc([item.job_title,item.message].filter(Boolean).join(' · '))}</span></a><time>${esc(day(item.since))}</time></li>`).join('')}</ul>` : '';
    host.innerHTML = `<button type="button" class="needs-bar" aria-expanded="${ui.open}" data-needs-toggle><b>需要你出手 ${items.length}</b><span>${esc(summary)}</span><i aria-hidden="true">${ui.open ? '收起' : '查看'}</i></button>${rows}`;
    host.querySelector('[data-needs-toggle]').onclick = () => {ui.open = !ui.open;paint(host,data,{url});};
  }

  return {paint};
})();
