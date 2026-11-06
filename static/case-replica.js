"use strict";

// 案例页顶栏的「照原片做视频」：确认原稿和镜头数，创建制作单后进入配音与剪辑。
(() => {
  const button = document.querySelector("#replica-top");
  const caseId = () => /^#(?:\/[^/]+\/)?case\/(PVR-[0-9a-f]{12})/.exec(location.hash)?.[1];
  const jobURL = job => window.WorkbenchShell?.url(`#production/${encodeURIComponent(job.id)}`, job.product) || `#production/${encodeURIComponent(job.id)}`;
  const seconds = value => (typeof value === "number" ? value.toFixed(1) : "");
  const DEFAULT_OPTIMIZED = 2;

  function warnings(preview) {
    const list = [];
    if (preview.machine_transcript) list.push("口播是机器转写，没有人工听校。有错先到「编辑文案」改好，再回来创建。");
    if (!preview.timed) list.push("原稿没有时间戳，每句对应的原片时间按字数估算，镜头切点可能有偏差。");
    if (preview.origin && preview.origin !== "self_produced") list.push("这条不是自制内容，文案里的品牌和说法会原样进入配音，请确认可以照用。");
    return list;
  }

  function render(dialog, preview) {
    const notes = warnings(preview), jobs = preview.jobs;
    dialog.innerHTML = `<div class="dialog-heading"><div><h2 id="replica-title">照原片做视频</h2></div><button type="button" class="icon-button" data-close aria-label="关闭">${icon("close")}</button></div>
      <div class="replica-body">
        <p>默认用原片稿：文案逐句用原稿（${preview.lines.length} 句）。画面按原片 ${preview.shots} 个镜头的顺序和切点找素材（原片 ${seconds(preview.duration)} 秒）。下一步在制作单选音色、配音、建时间线，AI 剪辑默认用「1:1 复刻原片」。</p>
        ${notes.length ? `<ul class="replica-notes">${notes.map(note => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
        <ol class="replica-lines">${preview.lines.map(line => `<li><span>${esc(line.spoken)}</span>${line.zh ? `<small>${esc(line.zh)}</small>` : ""}</li>`).join("")}</ol>
        <label class="replica-optimize">另写优化稿 <select data-optimize>${[0, 1, 2, 3, 4, 5].map(count => `<option value="${count}"${count === DEFAULT_OPTIMIZED ? " selected" : ""}>${count ? `${count} 条` : "不写"}</option>`).join("")}</select>
          <small>按写稿技能逐句优化（句数和句序不变，修掉没依据的说法，补好收口），写完到「内容创作」采纳，点「制作视频」同样按原片 1:1 剪。</small></label>
        ${jobs.length ? `<p class="replica-existing">这条案例已有制作单：${jobs.map(job => `<a href="${esc(jobURL(job))}" data-close>${esc(job.id)}</a>`).join("、")}</p>` : ""}
        <p class="ai-error" data-error role="alert" hidden></p>
      </div>
      <div class="dialog-actions replica-actions"><button type="button" class="secondary" data-close>取消</button>${jobs.length
        ? '<button type="button" class="secondary" data-replica="new">再建一张</button><button type="button" class="primary" data-replica="continue">继续已有制作单</button>'
        : '<button type="button" class="primary" data-replica="new">创建制作单</button>'}</div>`;
    dialog.querySelectorAll("[data-close]").forEach(item => item.addEventListener("click", () => dialog.close()));
    const buttons = [...dialog.querySelectorAll("[data-replica]")];
    buttons.forEach(button => button.onclick = async () => {
      const optimize = Number(dialog.querySelector("[data-optimize]").value) || 0;
      buttons.forEach(item => { item.disabled = true; });
      const error = dialog.querySelector("[data-error]");
      error.hidden = true;
      try {
        const result = await api(`/api/cases/${encodeURIComponent(preview.case_id)}/replica`,
          { method: "POST", body: { new: button.dataset.replica === "new", optimize } });
        dialog.close();
        const made = result.reused ? "已打开原片稿制作单" : "已创建原片稿制作单，选好音色就可以配音";
        if (result.task_error) toast(`${made}；优化稿没能开始：${result.task_error}`, true);
        else toast(result.task ? `${made}；${optimize} 条优化稿在写，写完到「内容创作」采纳。` : `${made}。`);
        location.hash = jobURL(result.job);
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
        buttons.forEach(item => { item.disabled = false; });
      }
    });
  }

  async function open() {
    const id = caseId();
    if (!id || !button || button.disabled) return;
    button.disabled = true;
    try {
      const preview = await api(`/api/cases/${encodeURIComponent(id)}/replica`);
      if (caseId() !== id) return;
      let dialog = document.querySelector("#replica-dialog");
      if (!dialog) {
        dialog = document.createElement("dialog");
        dialog.id = "replica-dialog";
        dialog.setAttribute("aria-labelledby", "replica-title");
        document.body.append(dialog);
      }
      render(dialog, preview);
      dialog.showModal();
    } catch (failure) {
      toast(failure.message, true);
    } finally {
      button.disabled = false;
    }
  }

  button?.addEventListener("click", open);
})();
