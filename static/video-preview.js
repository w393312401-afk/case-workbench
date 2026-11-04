'use strict';

window.VideoPreview = (() => {
  const pending = new Map();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const markup = uid => `<section class="video-preview" data-video-preview="${esc(uid)}"><div class="video-preview-toolbar"><h3>视频预览</h3><button type="button" class="secondary" data-preview-generate>生成预览</button><a class="secondary" data-preview-download hidden>下载 MP4</a></div><p class="context-note" role="status" data-preview-status>正在读取预览状态…</p><progress max="100" value="0" aria-label="预览导出进度" data-preview-progress hidden></progress><video controls playsinline preload="metadata" data-preview-player hidden></video><p class="context-note" data-preview-version hidden></p></section>`;

  async function bind(host,{job,uid,api}) {
    const root = host.querySelector('[data-video-preview]');if (!root) return;
    const token = Symbol('preview');root._previewBind = token;
    const current = () => root.isConnected && root._previewBind === token;
    const base = `/api/production/jobs/${encodeURIComponent(job.id)}/preview`, key = `${job.id}:${uid}`;
    const button = root.querySelector('[data-preview-generate]'), status = root.querySelector('[data-preview-status]');
    const progress = root.querySelector('[data-preview-progress]'), player = root.querySelector('[data-preview-player]');
    const download = root.querySelector('[data-preview-download]'), version = root.querySelector('[data-preview-version]');
    let data = null, timer = null, posting = false, generation = 0;
    const running = () => ['queued','running'].includes(data?.task?.status);
    function paint() {
      if (!current() || !data) return;
      const busy = running(), task = data.task, preview = data.preview;
      if (task?.request_id === pending.get(key)) pending.delete(key);
      button.disabled = posting || busy || Boolean(data.blocked_reason);
      button.title = data.blocked_reason || '';
      button.textContent = busy ? '正在生成预览…' : preview ? '重新生成预览' : '生成预览';
      status.className = task?.status === 'failed' ? 'ai-error' : 'context-note';
      status.textContent = task?.status === 'failed' ? task.error || '预览生成失败，请重试。' : busy ? task.message || '正在导出预览…'
        : preview ? preview.warning || '预览已生成，可在线播放或下载 MP4。' : data.blocked_reason || '由工作台所在电脑导出，生成后可在当前浏览器播放。';
      progress.hidden = !busy;progress.value = Math.max(0,Math.min(100,Number(task?.percent) || 0));
      player.hidden = download.hidden = version.hidden = !preview;
      if (preview) {
        if (player.dataset.previewId !== preview.id) {player.src = preview.url;player.dataset.previewId = preview.id;}
        download.href = preview.url+'?download=1';
        const date = preview.finished_at ? new Date(preview.finished_at).toLocaleString() : '';
        version.className = preview.source_current ? 'context-note' : 'ai-error';
        version.textContent = `${preview.width} × ${preview.height} · 生成于 ${date}。${preview.source_current ? '修改时间线后请重新生成预览。' : '制作单已有后续修改，请重新生成以查看最新版本。'}`;
      }
    }
    function schedule() {
      clearTimeout(timer);if (current() && running()) timer = setTimeout(load,1500);
    }
    async function load() {
      if (!current()) return;
      const readGeneration = generation;
      try {const next = await api(`${base}?timeline=${encodeURIComponent(uid)}`);if (!current() || readGeneration !== generation) return;data = next;paint();}
      catch (error) {if (current()) {status.className = 'ai-error';status.textContent = error.message || '预览状态暂时无法读取。';}}
      finally {schedule();}
    }
    button.onclick = async () => {
      if (posting || running()) return;
      posting = true;generation += 1;clearTimeout(timer);button.disabled = true;
      status.className = 'context-note';status.textContent = '正在提交预览导出…';
      const request = pending.get(key) || crypto.randomUUID();pending.set(key,request);let failed = false;
      try {
        const next = await api(base,{method:'POST',body:{request_id:request,timeline_uid:uid}});
        pending.delete(key);if (!current()) return;data = next;
      } catch (error) {
        failed = true;
        if (Number.isInteger(error.status) && error.status >= 400 && error.status < 500) pending.delete(key);
        if (current()) {status.className = 'ai-error';status.textContent = error.message || '未能确认导出是否启动，请重试。';}
      } finally {posting = false;if (current()) {button.disabled = running() || Boolean(data?.blocked_reason);if (data && !failed) paint();schedule();}}
    };
    player.addEventListener('error',() => {status.className = 'ai-error';status.textContent = '视频读取失败，请刷新确认登录状态，或下载 MP4 查看。';});
    await load();
  }
  return {markup,bind};
})();
