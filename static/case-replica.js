"use strict";

// 案例页顶栏的「照原片做视频」：确认原稿和镜头数，创建制作单后进入配音与剪辑。
(() => {
  const button = document.querySelector("#replica-top");
  const caseId = () => /^#(?:\/[^/]+\/)?case\/(PVR-[0-9a-f]{12})/.exec(location.hash)?.[1];
  const jobURL = job => window.WorkbenchShell?.url(`#production/${encodeURIComponent(job.id)}`, job.product) || `#production/${encodeURIComponent(job.id)}`;
  const seconds = value => (typeof value === "number" ? value.toFixed(1) : "");

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
        <p>文案逐句用原稿（${preview.lines.length} 句）。画面按原片 ${preview.shots} 个镜头的顺序和切点找素材（原片 ${seconds(preview.duration)} 秒）。下一步在制作单选音色、配音、建时间线，AI 剪辑默认用「1:1 复刻原片」。</p>
        ${notes.length ? `<ul class="replica-notes">${notes.map(note => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
        <ol class="replica-lines">${preview.lines.map(line => `<li><span>${esc(line.spoken)}</span>${line.zh ? `<small>${esc(line.zh)}</small>` : ""}</li>`).join("")}</ol>
        ${jobs.length ? `<p class="replica-existing">这条案例已有制作单：${jobs.map(job => `<a href="${esc(jobURL(job))}" data-close>${esc(job.id)}</a>`).join("、")}</p>` : ""}
        <p class="ai-error" data-error role="alert" hidden></p>
      </div>
      <div class="dialog-actions replica-actions"><button type="button" class="secondary" data-close>取消</button>${jobs.length
        ? `<button type="button" class="secondary" data-replica-new>再建一张</button><a class="primary" href="${esc(jobURL(jobs[jobs.length - 1]))}" data-close>继续已有制作单</a>`
        : '<button type="button" class="primary" data-replica-new>创建制作单</button>'}</div>`;
    dialog.querySelectorAll("[data-close]").forEach(item => item.addEventListener("click", () => dialog.close()));
    const create = dialog.querySelector("[data-replica-new]");
    create.onclick = async () => {
      create.disabled = true;
      const error = dialog.querySelector("[data-error]");
      error.hidden = true;
      try {
        const result = await api(`/api/cases/${encodeURIComponent(preview.case_id)}/replica`, { method: "POST", body: { new: true } });
        dialog.close();
        toast("已创建制作单，选好音色就可以配音。");
        location.hash = jobURL(result.job);
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
        create.disabled = false;
      }
    };
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
