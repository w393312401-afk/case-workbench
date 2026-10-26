"use strict";

function field(name, label, value = "", options = {}) {
  let control;
  if (options.select)
    control = `<select name="${name}">${Object.entries(options.select)
      .map(
        ([v, l]) =>
          `<option value="${esc(v)}" ${String(value) === v ? "selected" : ""}>${esc(l)}</option>`,
      )
      .join("")}</select>`;
  else if (options.area)
    control = `<textarea name="${name}" class="${options.script ? "script-input" : ""}" ${options.required ? "required" : ""}>${esc(value)}</textarea>`;
  else
    control = `<input name="${name}" value="${esc(value)}" ${options.readonly ? "readonly" : ""} ${options.required ? "required" : ""} ${options.type === "number" ? 'type="number" min="0" step="1"' : options.type === "date" ? 'type="date"' : ""} ${options.placeholder ? `placeholder="${esc(options.placeholder)}"` : ""}>`;
  return `<label class="field"><span>${label}${options.required ? " *" : ""}</span>${control}${options.help ? `<small>${esc(options.help)}</small>` : ""}</label>`;
}
function assessmentFields(current, dim, label) {
  const assessment =
    (current.assessments || []).find((x) => x.dimension === dim) || {};
  return (
    field("polarity_" + dim, label, assessment.polarity || "unknown", {
      select: polarityNames,
    }) + field("reason_" + dim, "评价依据", assessment.reason || "")
  );
}
const categoryChoices = {
  转化正向参考: "转化正向参考",
  高播放低转化: "高播放低转化",
  待验证与整理: "待验证与整理",
};
function categoryOptions(value) {
  return Object.hasOwn(categoryChoices, value)
    ? categoryChoices
    : { ...categoryChoices, [value]: `保留原分类：${value}` };
}
function categoryView(value) {
  return (
    {
      转化正向参考: "positive",
      高播放低转化: "mixed",
      待验证与整理: "pending",
    }[value] || null
  );
}
function renderCategoryFeedback() {
  const node = $("#category-feedback"),
    editor = state.editor;
  if (!node || !editor) return;
  const category = $('[name="category"]')?.value;
  const polarity = $('[name="polarity_conversion"]')?.value;
  const warnings = [];
  let conflict = false;
  if (category === "转化正向参考" && polarity !== "positive") {
    conflict = true;
    warnings.push("当前转化评价尚未标为正向，请核对分类和评价依据。");
  }
  if (category === "高播放低转化" && polarity === "positive") {
    conflict = true;
    warnings.push("当前转化评价为正向，与“高播放低转化”分类不一致。");
  }
  if (category === "待验证与整理" && polarity === "positive") {
    conflict = true;
    warnings.push("当前转化评价为正向，与“待验证与整理”分类不一致。");
  }
  const manual = editor.item && state.navigation[editor.item.case_id];
  if (manual && category !== editor.current.category && categoryView(category))
    warnings.push(`保存后栏目归属也会改为“${category}”。`);
  else if (manual && !categoryView(category))
    warnings.push("此案例有单独设置的栏目归属；保留旧分类不会改动归属。");
  node.textContent = warnings.join(" ");
  node.classList.toggle("warning", conflict);
  node.classList.toggle("info", !conflict);
  node.hidden = !warnings.length;
}
function openEditor(item, options = {}) {
  if (item && !options.advanced && window.CaseInlineEditor)
    return window.CaseInlineEditor.open(item, options.sectionId || "overview");
  if (window.CaseAI?.modalOpen() || window.CaseScripts?.modalOpen?.() || window.MaterialBasket?.modalOpen?.()) {
    toast("请先完成或关闭 AI 协作窗口。", true);
    return false;
  }
  if (state.editor) {
    toast(
      state.editor.uploads
        ? "视频或附件正在复制，请稍候。"
        : "请先保存或关闭当前编辑。",
      true,
    );
    return false;
  }
  if (state.deletion || state.deleteOperation || state.bulk) {
    toast("请先完成或关闭当前批量操作或删除确认。", true);
    return;
  }
  if (state.archiveOperation) {
    toast("正在更新归档状态，请稍候。");
    return;
  }
  const current = structuredClone(
    item?.current || {
      title: "",
      category: "待验证与整理",
      conclusion: "",
      kind: "editorial",
      assessments: [],
      evidence: { status: "unverified" },
    },
  );
  state.editor = {
    item: structuredClone(item),
    current,
    dirty: false,
    uploads: 0,
    videoUpload: null,
    mode: item || options.advanced ? "advanced" : "create",
  };
  if (!item && !options.advanced) { renderQuickEditor(); return true; }
  $("#edit-dialog").classList.remove("quick-create-dialog");
  const m = current.metrics || {},
    t = current.transcript || {},
    translation = current.translation_zh || {};
  $("#edit-title").textContent = item
    ? item.archived
      ? "编辑案例 · 已归档"
      : "编辑案例"
    : "收录新案例";
  $("#save-hint").textContent = item?.archived
    ? "保存会新增一条修订，并保持已归档状态。"
    : "保存会新增一条修订，保留旧记录。";
  $("#save-hint").classList.remove("save-error");
  $("#save-button").disabled = false;
  $("#editor-fields").innerHTML =
    `<div class="editor-grid"><div><section class="editor-section"><h3>01 / 案例与结论</h3>${field("title", "案例标题", current.title, { required: true })}<div class="form-row">${field("product", "产品", item?.product || "", { required: true, readonly: !!item })}${field("category", "案例分类", current.category, { required: true, select: categoryOptions(current.category), help: "选择表现分类；旧写法可原样保留。" })}</div><p id="category-feedback" class="category-feedback" role="status" hidden></p>${field("purchase_reason", "主要购买理由", current.purchase_reason || "", { area: true, help: "一句话说明为什么值得拥有；不要填销量评价。" })}${field("conclusion", "当前结论", current.conclusion, { required: true, area: true })}<div class="form-row">${field("kind", "评价性质", current.kind, { select: kindNames })}</div><details class="advanced"><summary>观看与转化的分别评价</summary><div class="form-row"><div>${assessmentFields(current, "reach", "播放表现")}</div><div>${assessmentFields(current, "conversion", "转化表现")}</div></div></details></section><section class="editor-section"><h3>02 / 当前有效文案</h3>${field("transcript_text", "完整文案", t.text || "", { area: true, script: true })}${field("transcript_status", "文案核实状态", t.status || "", { placeholder: "例如：用户提供，未逐字核对视频" })}${field("transcript_source", "文案来源", t.source || "", { placeholder: "文件路径或来源说明" })}</section><section class="editor-section"><h3>03 / 可复用的判断</h3>${field("highlights", "可借鉴的表达", (current.highlights || []).join("\n"), { area: true, help: "每行一条" })}${field("limitations", "问题与适用边界", (current.limitations || []).join("\n"), { area: true, help: "每行一条" })}</section></div><div><section class="editor-section"><h3>04 / 本地视频与附件</h3><button type="button" class="video-dropzone" id="video-dropzone" data-video-drop data-pick="video" aria-describedby="video-import-message">${icon("video")}<strong>拖入视频，或点击选择</strong><span>一次一个 · MP4 / WebM / MOV / M4V · 最大 2 GB</span></button><div class="video-import-status" id="video-import-status" role="status" aria-live="polite"><progress id="video-import-progress" max="100" value="0" aria-label="视频复制进度" hidden></progress><span id="video-import-message"></span></div><label class="field"><span>当前有效视频</span><input name="asset_video" value="${esc(current.assets?.video || "")}" placeholder="也可填写本地视频完整路径，保存时自动复制"><small>保存后，副本放入此案例对应的视频文件夹。</small></label><input class="file-chooser" type="file" data-upload="video" accept="${videoAccept}"><p>其他附件：填路径关联，或选择文件复制到案例库。</p>${Object.entries(
      assetLabels,
    )
      .filter(([kind]) => !["video", "screenshot"].includes(kind))
      .map(
        ([kind, label]) =>
          `<label class="field"><span>${label}</span><div class="input-actions"><input name="asset_${kind}" value="${esc(current.assets?.[kind] || "")}" placeholder="本地文件完整路径"><button type="button" class="upload-button" data-pick="${kind}">选择文件</button></div><input class="file-chooser" type="file" data-upload="${kind}" accept="${kind === "video" ? ".mp4,.webm,.mov,.m4v" : kind === "screenshot" || kind === "contact_sheet" ? ".png,.jpg,.jpeg,.webp" : kind === "source_manifest" ? ".json" : ".txt,.md,.json"}"></label>`,
      )
      .join(
        "",
      )}</section>${releaseEditor(current)}<section class="editor-section"><h3>06 / 关联销售数据</h3><p>表现数据按视频 ID 从销售数据表读取，修改案例不会改写销售数据。</p>${field("metric_video_id", "视频 ID", m.video_id || "")}${field("metric_creator_handle", "发布账号", m.creator_handle || "")}${field("metric_published_title", "发布标题", m.published_title || "")}${!item ? field("url", "原视频链接", "", { placeholder: "https://…" }) : ""}<p class="context-note">${item ? esc(salesStatusLabel(item)) : "保存后按视频 ID 匹配销售数据。"}</p><a class="secondary" href="${esc(salesDataURL(item || {current}))}" target="_blank">查看销售数据表</a></section><section class="editor-section"><h3>07 / 本次修订</h3>${field("note", "修改说明", item ? "" : "首次收录案例", { required: true, area: true, help: "说明补了什么数据、改了哪项判断或重新关联了哪个视频。" })}</section></div></div>`;
  $('[name="title"]').closest(".field").insertAdjacentHTML(
    "afterend",
    field("content_origin", "内容来源", current.content_origin || "", {
      select: { "": "内容来源待确认", ...contentOriginNames },
      help: "自制：国内自己制作。达人：海外本土达人制作，可销售我们的产品或其他产品。",
    }),
  );
  $('[name="purchase_reason"]').closest(".field").insertAdjacentHTML(
    "afterend", window.CaseDirectionPicker.markup(current),
  );
  $('[name="transcript_source"]').closest(".field").insertAdjacentHTML(
    "afterend",
    `<div class="translation-editor"><h4>中文口语对照</h4><p>译文与当前原文一起保存。改动原文后，旧译文会从当前版本移除，仍保留在修订历史中。</p>${field("translation_channel", "原文类型", translation.channel || (t.text ? "transcript" : "on_screen"), { select: { transcript: "完整文案", on_screen: "屏显文字", historical_excerpt: "历史文字片段" } })}${field("translation_source_text", "对应原文", translation.source_text || t.text || "", { area: true, help: "完整文案自动使用上方原文；屏显文字和历史片段请填写实际文字。" })}${field("translation_text", "中文口语译文", translation.text || "", { area: true, script: true })}</div>`,
  );
  const syncTranslationSource = () => {
    const channel = $('[name="translation_channel"]').value;
    const source = $('[name="translation_source_text"]');
    source.readOnly = channel === "transcript";
    if (channel === "transcript") source.value = $('[name="transcript_text"]').value;
    source.closest(".field").classList.toggle("translation-source-linked", channel === "transcript");
  };
  $('[name="translation_channel"]').addEventListener("change", () => {
    if (translation.channel && $('[name="translation_channel"]').value !== translation.channel &&
        $('[name="translation_text"]').value.trim() === translation.text)
      $('[name="translation_text"]').value = "";
    syncTranslationSource();
  });
  $('[name="transcript_text"]').addEventListener("input", () => {
    syncTranslationSource();
    if (translation.channel === "transcript" && $('[name="transcript_text"]').value.trim() !== translation.source_text &&
        $('[name="translation_text"]').value.trim() === translation.text)
      $('[name="translation_text"]').value = "";
  });
  $('[name="translation_source_text"]').addEventListener("input", () => {
    if (translation.channel !== "transcript" && translation.source_text && $('[name="translation_source_text"]').value.trim() !== translation.source_text &&
        $('[name="translation_text"]').value.trim() === translation.text)
      $('[name="translation_text"]').value = "";
  });
  syncTranslationSource();
  $("#editor-fields .editor-grid > div").insertAdjacentHTML(
    "beforeend",
    nativeEditor(current),
  );
  bindNativeEditor();
  $$("[data-pick]").forEach(
    (button) =>
      (button.onclick = () =>
        $(`[data-upload="${button.dataset.pick}"]`).click()),
  );
  $$("[data-upload]").forEach(
    (input) => (input.onchange = () => uploadFile(input)),
  );
  $('[name="category"]').addEventListener("change", renderCategoryFeedback);
  $('[name="polarity_conversion"]').addEventListener(
    "change",
    renderCategoryFeedback,
  );
  renderCategoryFeedback();
  window.CaseDirectionPicker.bind($("#edit-form"), item || {product: $('[name="product"]').value, current});
  $("#edit-dialog").showModal();
  renderVideoImportStatus();
  return true;
}
function defaultCaseProduct() {
  const selected = window.WorkbenchShell?.product();
  if (selected) return selected;
  try { const last = localStorage.getItem("workbench-last-created-product"); if (last) return last; } catch {}
  const first = (state.cases || []).find(item => item.product)?.product;
  return first ? window.WorkbenchShell?.normalizeProduct?.(first) || first : "202H";
}
function videoEditorFields(current, attachments = false) {
  return `<button type="button" class="video-dropzone" id="video-dropzone" data-video-drop data-pick="video" aria-describedby="video-import-message">${icon("video")}<strong>拖入本地视频，或点击选择</strong><span>可代替原视频链接 · MP4 / WebM / MOV / M4V · 最大 2 GB</span></button><div class="video-import-status" id="video-import-status" role="status" aria-live="polite"><progress id="video-import-progress" max="100" value="0" aria-label="视频复制进度" hidden></progress><span id="video-import-message"></span></div><details class="advanced"><summary>本地视频路径${attachments ? "与其他附件" : "（可选）"}</summary>${field("asset_video", "当前有效视频", current.assets?.video || "", {placeholder: "也可填写本地视频完整路径"})}${attachments ? Object.entries(assetLabels).filter(([kind]) => !["video", "screenshot"].includes(kind)).map(([kind, label]) => `<label class="field"><span>${esc(label)}</span><div class="input-actions"><input name="asset_${esc(kind)}" value="${esc(current.assets?.[kind] || "")}"><button type="button" class="upload-button" data-pick="${esc(kind)}">选择文件</button></div></label><input type="file" class="file-chooser" data-upload="${esc(kind)}">`).join("") : ""}</details><input class="file-chooser" type="file" data-upload="video" accept="${videoAccept}">`;
}
function bindEditorUploads(root) {
  $$("[data-pick]", root).forEach(button => button.onclick = () => $(`[data-upload="${button.dataset.pick}"]`, root)?.click());
  $$("[data-upload]", root).forEach(input => input.onchange = () => uploadFile(input));
  $('[name="asset_video"]', root)?.addEventListener("input", () => {
    if (!state.editor) return;
    state.editor.videoUpload = null;
    renderVideoImportStatus();
    syncQuickSource();
  });
}
function syncQuickSource() {
  if (state.editor?.mode !== "create") return;
  const url = $('[name="url"]', $("#edit-form"));
  if (url) url.required = !Boolean($('[name="asset_video"]', $("#edit-form"))?.value.trim());
}
function renderQuickEditor() {
  const editor = state.editor, product = defaultCaseProduct();
  $("#edit-title").textContent = "收录新案例";
  $("#edit-dialog").classList.add("quick-create-dialog");
  $("#save-hint").textContent = "先记下这三项，文案、方向和数据可以保存后补充。";
  $("#save-hint").classList.remove("save-error");
  $("#save-button").disabled = false;
  $("#editor-fields").innerHTML = `<div class="quick-create-fields"><p class="quick-create-product">产品：<strong>${esc(product)}</strong></p>${field("title", "案例标题", "", {required: true})}${field("url", "原视频链接", "", {required: true, placeholder: "https://…", help: "也可以在下方导入一个本地视频。"})}${field("conclusion", "一句话结论", "", {required: true, area: true, help: "记录这条案例值得留意的地方。"})}<div class="quick-create-media">${videoEditorFields(editor.current)}</div><input type="hidden" name="product" value="${esc(product)}">${["content_origin", "metric_video_id", "metric_creator_handle", "metric_published_title", "publish_published_on"].map(name => `<input type="hidden" name="${name}" value="">`).join("")}</div>`;
  bindEditorUploads($("#edit-form"));
  $('[name="product"]', $("#edit-form")).addEventListener("change", event => {
    $(".quick-create-product strong").textContent = event.target.value;
  });
  $("#edit-dialog").showModal();
  renderVideoImportStatus();
  $('[name="title"]', $("#edit-form")).focus();
}
function collectQuickCurrent() {
  const values = new FormData($("#edit-form")), value = name => String(values.get(name) || "").trim();
  const current = structuredClone(state.editor.current);
  current.title = value("title"); current.conclusion = value("conclusion");
  if (!current.title || !current.conclusion) throw new Error("请填写案例标题和一句话结论。");
  const url = value("url"), video = value("asset_video");
  if (!url && !video) throw new Error("请填写原视频链接，或导入一个本地视频。");
  if (url && !/^https?:\/\//i.test(url)) throw new Error("原视频链接须以 https:// 或 http:// 开头。");
  if (video) current.assets = {...(current.assets || {}), video};
  if (value("content_origin")) current.content_origin = value("content_origin");
  const metrics = {...(current.metrics || {})};
  for (const key of ["video_id", "creator_handle", "published_title"]) if (value("metric_" + key)) metrics[key] = value("metric_" + key);
  if (Object.keys(metrics).length) current.metrics = metrics;
  if (value("publish_published_on")) current.publish = {published_on: value("publish_published_on")};
  return {current, product: value("product") || defaultCaseProduct(), url, note: "首次收录案例"};
}
function videoImportError(files) {
  if (files.length !== 1)
    return "每次只能导入一个视频，请重新选择；本次没有导入任何文件。";
  const file = files[0];
  if (!/\.(mp4|webm|mov|m4v)$/i.test(file.name || ""))
    return "请选择 MP4、WebM、MOV 或 M4V 视频文件。";
  if (!file.size) return "这个视频是空文件，无法导入，请重新选择。";
  if (file.size > maxVideoBytes) return "视频不能超过 2 GB，请选择较小的视频。";
  return "";
}
function renderVideoImportStatus() {
  const editor = state.editor,
    box = $("#video-import-status");
  if (!editor || !box) return;
  const upload = editor.videoUpload,
    path = $('[name="asset_video"]')?.value.trim() || "";
  const status = $("#video-import-message"),
    progress = $("#video-import-progress");
  box.classList.toggle("has-error", upload?.state === "error");
  box.classList.toggle(
    "is-success",
    upload?.state === "complete" && upload.path === path,
  );
  progress.hidden = upload?.state !== "uploading";
  if (upload?.state === "uploading") {
    if (upload.progress === null) progress.removeAttribute("value");
    else progress.value = upload.progress;
    status.textContent = `${upload.name} · ${upload.progress === 100 ? "复制已传输完成，正在核对文件…" : `正在复制${upload.progress === null ? "…" : ` ${upload.progress}%`}`}。完成前请保持此窗口打开。`;
  } else if (upload?.state === "error")
    status.textContent = `${upload.error} 原有视频和其他填写内容已保留，可重新选择。`;
  else if (upload?.state === "complete" && upload.path === path)
    status.textContent = `${upload.name} · 已复制，保存后归入本案例。`;
  else if (
    path &&
    path === editor.current.assets?.video &&
    editor.item?.assets?.video?.owned
  )
    status.textContent = editor.item.assets.video.exists
      ? "已保存案例库副本，原文件移动或删除不影响该视频。"
      : "案例库视频副本暂不可用，请重新导入视频。";
  else if (path)
    status.textContent = "保存时会把视频复制到本案例文件夹，原文件保持不变。";
  else
    status.textContent = "复制一份到案例库，原文件移动或删除不影响已保存视频。";
  $("#video-dropzone")?.setAttribute(
    "aria-busy",
    String(upload?.state === "uploading"),
  );
}
function editorForm(editor = state.editor) { return editor?.form || $("#edit-form"); }
function updateUploadControls(editor) {
  if (state.editor !== editor) return;
  const busy = !!editor.uploads || !!editor.saving, blocked = busy || !!editor.deleted;
  $$("[data-pick], [data-upload]", editorForm(editor)).forEach(control => control.disabled = blocked);
  const asset = $('[name="asset_video"]', editorForm(editor));
  if (asset) asset.disabled = blocked;
  const save = editor.mode === "inline" ? $("[data-inline-save]", editor.form) : $("#save-button");
  if (save) save.disabled = blocked;
  if (editor.mode === "inline") {
    $$("[data-inline-cancel]", editor.form).forEach(control => control.disabled = busy);
  } else {
    $("#close-editor").disabled = busy;
    $("#cancel-editor").disabled = busy;
  }
}
function uploadBinary(file, kind, editor, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    editor.uploadRequest = request;
    request.open(
      "POST",
      `/api/upload?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(file.name)}`,
    );
    request.setRequestHeader("X-Workbench-Token", state.token);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = (event) =>
      onProgress?.(
        event.lengthComputable
          ? Math.min(100, Math.round((event.loaded / event.total) * 100))
          : null,
      );
    request.onload = () => {
      let result;
      try {
        result = JSON.parse(request.responseText);
      } catch {
        reject(new Error("未能确认复制结果，请重试。"));
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(result.error || "复制失败，请重试。"));
        return;
      }
      if (!result.path || typeof result.path !== "string") {
        reject(new Error("返回的文件信息不完整，请重试。"));
        return;
      }
      resolve(result);
    };
    request.onerror = () =>
      reject(new Error("连接中断，未能确认复制完成，请重新选择视频。"));
    request.onabort = () => reject(new Error("复制已停止。"));
    request.send(file);
  });
}

async function uploadAttachment(file, kind) {
  const editor = state.editor;
  if (!editor || editor.deleted) {
    toast("此案例已删除，不能继续添加附件。", true);
    return;
  }
  if (editor.uploads || editor.saving) {
    toast("正在复制或保存，请完成后再导入。", true);
    return;
  }
  if (!$(`[name="asset_${kind}"]`, editorForm(editor))) {
    toast("请先保存当前分区，再到视频与附件中导入。", true);
    return;
  }
  editor.uploads = 1;
  if (kind === "video")
    editor.videoUpload = { state: "uploading", name: file.name, progress: 0 };
  updateUploadControls(editor);
  renderVideoImportStatus();
  try {
    const result = await uploadBinary(file, kind, editor, (progress) => {
      if (state.editor !== editor || editor.deleted || kind !== "video") return;
      editor.videoUpload.progress = progress;
      renderVideoImportStatus();
    });
    if (state.editor !== editor || editor.deleted) return;
    $(`[name="asset_${kind}"]`).value = result.path;
    editor.dirty = true;
    if (kind === "video") {
      editor.videoUpload = {
        state: "complete",
        name: file.name,
        path: result.path,
      };
      const title = $('[name="title"]');
      if (title && !title.value.trim()) title.value = file.name.replace(/\.[^.]+$/, "");
      syncQuickSource();
    }
    toast(
      kind === "video"
        ? "视频已复制，保存后归入本案例"
        : "附件已复制；保存案例后完成关联",
    );
  } catch (error) {
    if (state.editor !== editor || editor.deleted) return;
    if (kind === "video")
      editor.videoUpload = { state: "error", error: error.message };
    toast(error.message, true);
  } finally {
    editor.uploads = 0;
    editor.uploadRequest = null;
    updateUploadControls(editor);
    renderVideoImportStatus();
  }
}
async function uploadFile(input) {
  const files = [...input.files];
  input.value = "";
  if (!files.length) return;
  if (input.dataset.upload === "video") {
    await importVideoFiles(files);
    return;
  }
  if (files.length !== 1) {
    toast("每次请选择一个附件。", true);
    return;
  }
  await uploadAttachment(files[0], input.dataset.upload);
}
function openVideoPicker(item = null) {
  if ($("#discard-dialog").open) {
    toast("请先选择继续编辑或放弃修改。", true);
    return;
  }
  if (!state.editor && !(item && window.CaseInlineEditor ? window.CaseInlineEditor.open(item, "assets") : openEditor(item))) return;
  if (state.editor?.uploads || state.editor?.saving || state.editor?.deleted) {
    toast("请等待当前操作完成后再导入。", true);
    return;
  }
  const input = $('[data-upload="video"]', editorForm());
  if (input) input.click();
  else toast("请先保存当前分区，再到视频与附件中导入。", true);
}
async function importVideoFiles(files, item = null) {
  const error = videoImportError(files);
  if (error) {
    toast(error, true);
    return;
  }
  if ($("#discard-dialog").open) {
    toast("请先选择继续编辑或放弃修改。", true);
    return;
  }
  if (!state.editor && !(item && window.CaseInlineEditor ? window.CaseInlineEditor.open(item, "assets") : openEditor(item))) return;
  await uploadAttachment(files[0], "video");
}
function isFileDrag(event) {
  return [...(event.dataTransfer?.types || [])].includes("Files");
}
function dragItem() {
  return caseLocation()?.id === state.detail?.case_id ? state.detail : null;
}
function hideVideoDrop() {
  $("#video-drop-overlay").hidden = true;
  $$("[data-video-drop]").forEach((zone) =>
    zone.classList.remove("drag-active"),
  );
}
const productionFields = [
  ["script_version", "文案版本", "例如：V2｜够用与进阶都备好"],
  ["davinci_project", "达芬奇项目", "例如：202H"],
  ["davinci_timeline", "达芬奇时间线", "例如：lab-260929-202-2"],
  ["voiceover", "配音文件", "文件名或完整路径"],
  ["source_ledger", "源区间账本", "davinci-ledger 下的 JSON 路径"],
];
function releaseEditor(current) {
  const production = current.production || {};
  const linked = productionFields.some(([key]) => production[key]);
  return `<section class="editor-section"><h3>05 / 发布与制作</h3><p>填写发布日期及制作关联，复盘时可以回到对应文案版本和剪辑时间线。</p>${field("publish_published_on", "发布日期", current.publish?.published_on || "", { type: "date" })}<details class="advanced" ${linked ? "open" : ""}><summary>制作关联（文案版本、达芬奇时间线等）</summary>${productionFields.map(([key, label, placeholder]) => field("production_" + key, label, production[key] || "", { placeholder })).join("")}</details></section>`;
}
function collectRelease(current, value) {
  if (value("publish_published_on")) current.publish = { published_on: value("publish_published_on") };
  else delete current.publish;
  const production = {};
  productionFields.forEach(([key]) => {
    if (value("production_" + key)) production[key] = value("production_" + key);
  });
  if (Object.keys(production).length) current.production = production;
  else delete current.production;
  const days = daysSincePublish(current);
  if (days !== null && days < 0) throw new Error("数据截至日不能早于发布日期。");
}
function collectCurrent() {
  if (state.editor?.mode === "create") return collectQuickCurrent();
  const values = new FormData($("#edit-form"));
  const value = (name) => String(values.get(name) ?? "").trim();
  const current = structuredClone(state.editor.current);
  ["title", "category", "conclusion", "kind"].forEach(
    (key) => (current[key] = value(key)),
  );
  if (value("content_origin")) current.content_origin = value("content_origin");
  else delete current.content_origin;
  if (value("purchase_reason"))
    current.purchase_reason = value("purchase_reason");
  else delete current.purchase_reason;
  const m = { ...(current.metrics || {}) };
  [
    "video_id",
    "creator_handle",
    "published_title",
  ].forEach((key) => {
    const v = value("metric_" + key);
    if (v === "") delete m[key];
    else m[key] = v;
  });
  current.metrics = m;
  // Hidden historical sales fields and screenshot references survive ordinary edits.
  const assets = { ...(current.assets || {}) };
  Object.keys(assetLabels).filter(kind => kind !== "screenshot").forEach(kind => {
    if (value("asset_" + kind)) assets[kind] = value("asset_" + kind);
    else delete assets[kind];
  });
  current.assets = assets;
  collectRelease(current, value);
  window.CaseDirectionPicker.collect(current, value, $("#edit-form"));
  if (value("transcript_text"))
    current.transcript = {
      text: value("transcript_text"),
      status: value("transcript_status") || "用户提供，未逐字核对视频",
      source: value("transcript_source") || "用户在本地案例工作台填写",
    };
  else delete current.transcript;
  const priorTranslation = state.editor.current.translation_zh;
  const translationChannel = value("translation_channel");
  const translationSource = translationChannel === "transcript"
    ? current.transcript?.text || ""
    : value("translation_source_text");
  const translationText = value("translation_text");
  if (priorTranslation && (
    priorTranslation.source_text !== translationSource ||
    priorTranslation.channel !== translationChannel
  ) && translationText === priorTranslation.text) {
    delete current.translation_zh;
  } else if (priorTranslation?.channel === "on_screen" &&
      state.editor.current.transcript?.text?.includes(priorTranslation.source_text) &&
      !current.transcript?.text?.includes(translationSource)) {
    delete current.translation_zh;
  } else if (translationText) {
    if (!translationSource) throw new Error("请先填写中文对照对应的原文。");
    current.translation_zh = {
      source_text: translationSource,
      text: translationText,
      channel: translationChannel,
    };
  } else {
    delete current.translation_zh;
  }
  ["highlights", "limitations"].forEach(
    (key) =>
      (current[key] = value(key)
        .split("\n")
        .map((x) => x.trim())
        .filter(Boolean)),
  );
  const retained = (current.assessments || []).filter(
    (x) => !["reach", "conversion"].includes(x.dimension),
  );
  for (const dim of ["reach", "conversion"]) {
    const polarity = value("polarity_" + dim),
      reason = value("reason_" + dim),
      old = (current.assessments || []).find((x) => x.dimension === dim);
    if (polarity !== "unknown" || reason || old)
      retained.push({
        dimension: dim,
        polarity,
        ...(reason ? { reason } : {}),
      });
  }
  current.assessments = retained;
  collectNativeReference(current, value);
  return {
    current,
    note: value("note"),
    product: value("product"),
    url: value("url"),
  };
}
async function saveEditor(event) {
  if (state.editor?.mode === "inline") return window.CaseInlineEditor.save(event);
  event.preventDefault();
  const editor = state.editor;
  if (!editor || editor.deleted || editor.uploads || editor.saving) return;
  let payload;
  try {
    payload = { ...collectCurrent(), revision: editor.item?.revision };
  } catch (error) {
    toast(error.message, true);
    return;
  }
  const fingerprint = JSON.stringify(payload);
  if (editor.fingerprint !== fingerprint) {
    editor.requestId = crypto.randomUUID();
    editor.fingerprint = fingerprint;
  }
  payload.request_id = editor.requestId;
  const button = $("#save-button");
  editor.saving = true;
  const controls = $$("input,textarea,select,button", $("#edit-form"));
  controls.forEach((el) => (el.disabled = true));
  button.textContent = "正在保存…";
  try {
    const result = await api(
      editor.item ? "/api/cases/" + editor.item.case_id : "/api/cases",
      { method: "POST", body: payload },
    );
    if (state.editor !== editor) return;
    let navigationWarning = "", autoNote = "";
    // 从视频数据「收为案例」新建的案例：保存后在后台下载原视频并拆解。
    if (editor.autoBreakdown && !editor.item && result.case?.case_id && window.CaseAutoBreakdown) {
      try {
        await window.CaseAutoBreakdown.start(result.case.case_id, editor.autoBreakdown);
        autoNote = "已保存，正在后台下载原视频并拆解，进度见视频下方。";
      } catch (error) {
        autoNote = `案例已保存，但自动下载拆解没有开始：${error.message}`;
      }
    }
    const target = categoryView(payload.current.category);
    if (
      editor.item &&
      !navigationAssignment(editor.item.case_id) &&
      target &&
      payload.current.category !== editor.current.category
    ) {
      try {
        await api("/api/navigation/move", {
          method: "POST",
          body: {
            case_id: editor.item.case_id,
            target,
            revision: result.case.revision,
            previous: state.navigation[editor.item.case_id] ?? null,
          },
        });
      } catch (error) {
        navigationWarning = `案例已保存，但栏目归属尚未同步：${error.message}`;
      }
    }
    editor.dirty = false;
    $("#edit-dialog").close();
    $("#editor-fields").innerHTML = "";
    state.editor = null;
    state.detailTab = "current";
    state.detail = result.case;
    try { localStorage.setItem("workbench-last-created-product", result.case.product || payload.product); } catch {}
    location.hash = window.WorkbenchShell?.url("#case/" + result.case.case_id, result.case.product || payload.product) || "#case/" + result.case.case_id;
    await refresh();
    toast(
      navigationWarning ||
        result.warning ||
        autoNote ||
        (result.unchanged
          ? "没有变更，记录保持原样"
          : "已保存到本地案例库，阅读目录已同步"),
      !!(navigationWarning || result.warning || autoNote.startsWith("案例已保存，但")),
    );
  } catch (error) {
    const duplicate = error.message.match(
      /视频 ID 已收录到 (PVR-[0-9a-f]{12})/,
    );
    if (duplicate) {
      $("#duplicate-case-note")?.remove();
      const note = document.createElement("div");
      note.id = "duplicate-case-note";
      note.className = "editor-conflict";
      note.innerHTML =
        '已有相同视频，当前输入已保留。<a target="_blank" rel="noopener" href="#case/' +
        esc(duplicate[1]) +
        '">打开已有案例补充数据</a>';
      $("#editor-fields").prepend(note);
    }
    $("#save-hint").textContent = error.message;
    $("#save-hint").classList.add("save-error");
    if (error.status === 409 && editor.item && !$("#conflict-note")) {
      const note = document.createElement("div");
      note.id = "conflict-note";
      note.className = "editor-conflict";
      note.innerHTML =
        '你的编辑已保留。<a target="_blank" rel="noopener" href="' +
        location.pathname +
        "#case/" +
        esc(editor.item.case_id) +
        '">在新页面查看最新案例</a>，核对后可关闭本编辑框并重新编辑。';
      $("#editor-fields").prepend(note);
    }
  } finally {
    editor.saving = false;
    controls.forEach((el) => (el.disabled = false));
    button.disabled = !!editor.deleted;
    button.innerHTML = icon("check") + "保存到本地案例库";
    if (editor.deleted)
      $$("[data-pick], [data-upload]", $("#edit-form")).forEach(
        (control) => (control.disabled = true),
      );
  }
}
function requestClose() {
  if (state.editor?.saving) {
    toast("正在保存，请稍候。");
    return;
  }
  if (state.editor?.uploads) {
    toast("附件正在复制，请稍候。", true);
    return;
  }
  if (state.editor?.dirty) {
    $("#discard-dialog").showModal();
    $("#keep-editing").focus();
  } else closeEditor();
}
function closeEditor() {
  if (state.editor?.mode === "inline") return window.CaseInlineEditor.close();
  if (state.editor?.uploads || state.editor?.saving) {
    toast("正在复制或保存，请稍候。", true);
    return;
  }
  if ($("#discard-dialog").open) $("#discard-dialog").close();
  $("#edit-dialog").close();
  $("#editor-fields").innerHTML = "";
  state.editor = null;
  if (state.pendingSync) {
    state.pendingSync = false;
    route(true);
  }
}
