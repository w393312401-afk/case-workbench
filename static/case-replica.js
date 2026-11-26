"use strict";

// 案例页顶栏的「照原片做视频」：确认原稿和镜头数，选用原稿开制作单或只写优化稿、选 AI 模型。
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

  const MODEL_KIND = "writing";
  const optimizeOptions = (min, chosen) => [0, 1, 2, 3, 4, 5].filter(count => count >= min)
    .map(count => `<option value="${count}"${count === chosen ? " selected" : ""}>${count ? `${count} 条` : "不写"}</option>`).join("");

  function render(dialog, preview) {
    const notes = warnings(preview), jobs = preview.jobs;
    dialog.innerHTML = `<div class="dialog-heading"><div><h2 id="replica-title">照原片做视频</h2></div><button type="button" class="icon-button" data-close aria-label="关闭">${icon("close")}</button></div>
      <div class="replica-body">
        <p>默认用原片稿：文案逐句用原稿（${preview.lines.length} 句）。画面按原片 ${preview.shots} 个镜头的顺序和切点找素材（原片 ${seconds(preview.duration)} 秒）。下一步在制作单选音色、配音、建时间线，AI 剪辑默认用「1:1 复刻原片」。</p>
        ${notes.length ? `<ul class="replica-notes">${notes.map(note => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
        <ol class="replica-lines">${preview.lines.map(line => `<li><span>${esc(line.spoken)}</span>${line.zh ? `<small>${esc(line.zh)}</small>` : ""}</li>`).join("")}</ol>
        <fieldset class="replica-mode"><legend>稿子怎么用</legend>
          <label><input type="radio" name="replica-mode" value="original" checked><span>用原稿开制作单<small>文案逐句用上面的原稿，马上可以配音；下面可以顺便另写几条优化稿备用。</small></span></label>
          <label><input type="radio" name="replica-mode" value="optimized"><span>只写优化稿，先不开制作单<small>不建原稿的单。写完到「内容创作」挑一条采纳，点「制作视频」再开单，同样按原片 1:1 剪。</small></span></label>
        </fieldset>
        <label class="replica-optimize"><span data-optimize-label>另写优化稿</span> <select data-optimize>${optimizeOptions(0, DEFAULT_OPTIMIZED)}</select>
          <small>每句仍对应原片那段画面，口播按说服链重写（句序不变；收口缺下单理由时在最后多加一句）。</small></label>
        <div class="replica-model"><span>AI 模型</span>${window.ModelPick ? window.ModelPick.markup(MODEL_KIND) : ""}
          <small data-model-note>用来写优化稿，也用来给原稿制作单自动补全意群表（每句放什么画面）。</small></div>
        ${jobs.length ? `<p class="replica-existing">这条案例已有制作单：${jobs.map(job => `<a href="${esc(jobURL(job))}" data-close>${esc(job.id)}</a>`).join("、")}</p>` : ""}
        <p class="ai-error" data-error role="alert" hidden></p>
      </div>
      <div class="dialog-actions replica-actions" data-actions></div>`;
    dialog.querySelectorAll("[data-close]").forEach(item => item.addEventListener("click", () => dialog.close()));
    void window.ModelPick?.mount(dialog);
    const mode = () => dialog.querySelector('input[name="replica-mode"]:checked')?.value || "original";
    const select = dialog.querySelector("[data-optimize]");

    function paintMode() {
      const original = mode() === "original", count = Number(select.value) || 0;
      // Without the original's job, at least one optimized script has to be written.
      select.innerHTML = optimizeOptions(original ? 0 : 1, original ? count : count || DEFAULT_OPTIMIZED);
      dialog.querySelector("[data-optimize-label]").textContent = original ? "另写优化稿" : "写几条优化稿";
      dialog.querySelector("[data-model-note]").textContent = original
        ? "用来写优化稿，也用来给原稿制作单自动补全意群表（每句放什么画面）。"
        : "用来写优化稿。";
      const actions = dialog.querySelector("[data-actions]");
      actions.innerHTML = `<button type="button" class="secondary" data-close>取消</button>${!original
        ? '<button type="button" class="primary" data-replica="draft">开始写优化稿</button>'
        : jobs.length
          ? '<button type="button" class="secondary" data-replica="new">再建一张</button><button type="button" class="primary" data-replica="continue">继续已有制作单</button>'
          : '<button type="button" class="primary" data-replica="new">创建制作单</button>'}`;
      actions.querySelector("[data-close]").addEventListener("click", () => dialog.close());
      actions.querySelectorAll("[data-replica]").forEach(button => button.onclick = () => submit(button));
    }

    async function submit(button) {
      const optimize = Number(select.value) || 0, draftOnly = button.dataset.replica === "draft";
      const model = window.ModelPick?.value(MODEL_KIND) || {};
      const buttons = [...dialog.querySelectorAll("[data-replica]")];
      buttons.forEach(item => { item.disabled = true; });
      const error = dialog.querySelector("[data-error]");
      error.hidden = true;
      try {
        const body = draftOnly ? { original: false, optimize, ...model } : { new: button.dataset.replica === "new", optimize, ...model };
        const result = await api(`/api/cases/${encodeURIComponent(preview.case_id)}/replica`, { method: "POST", body });
        if (result.task || (result.job && !result.reused)) window.ModelPick?.used(MODEL_KIND);
        dialog.close();
        if (draftOnly) {
          toast(`${optimize} 条优化稿在写，写完到「内容创作」挑一条采纳，点「制作视频」开单。`);
          return;
        }
        const made = result.reused ? "已打开原片稿制作单" : "已创建原片稿制作单，选好音色就可以配音";
        if (result.task_error) toast(`${made}；优化稿没能开始：${result.task_error}`, true);
        else toast(result.task ? `${made}；${optimize} 条优化稿在写，写完到「内容创作」采纳。` : `${made}。`);
        location.hash = jobURL(result.job);
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
        buttons.forEach(item => { item.disabled = false; });
      }
    }

    dialog.querySelectorAll('input[name="replica-mode"]').forEach(input => input.addEventListener("change", paintMode));
    paintMode();
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
