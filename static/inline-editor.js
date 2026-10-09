"use strict";

window.CaseInlineEditor = (() => {
  const labels = {overview: "概要与结论", transcript: "文案与中文对照", insights: "拆解要点", direction: "测试方向", release: "发布与制作", metrics: "视频与销售数据关联", assets: "视频与附件", native: "本土表达"};
  const legacySections = {overview: "conclusion", transcript: "transcript", insights: "highlights", direction: "test-direction", release: "release", metrics: "performance", assets: "video", native: "native"};
  const lines = value => value.split("\n").map(line => line.trim()).filter(Boolean);
  function fields(section, item) {
    const current = item.current, t = current.transcript || {}, tr = current.translation_zh || {}, m = current.metrics || {};
    switch (section) {
      case "overview": return `${field("title", "案例标题", current.title || item.title, {required: true})}${field("conclusion", "一句话结论", current.conclusion, {required: true, area: true})}${field("purchase_reason", "主要购买理由", current.purchase_reason || "", {area: true})}<div class="form-row">${field("category", "案例分类", current.category, {select: categoryOptions(current.category)})}${field("content_origin", "内容来源", current.content_origin || "", {select: {"": "内容来源待确认", ...contentOriginNames}})}</div>${field("kind", "评价性质", current.kind, {select: kindNames})}<p id="category-feedback" class="category-feedback" role="status" hidden></p><details class="advanced"><summary>观看与转化的分别评价</summary><div class="form-row"><div>${assessmentFields(current, "reach", "播放表现")}</div><div>${assessmentFields(current, "conversion", "转化表现")}</div></div></details>`;
      case "transcript": return `${field("transcript_text", "完整文案", t.text || "", {area: true, script: true})}${field("transcript_status", "文案核实状态", t.status || "")}${field("transcript_source", "文案来源", t.source || "")}<details class="advanced" ${tr.text ? "open" : ""}><summary>中文对照</summary><p>改动原文后，未同步修改的旧译文会从当前版本移除，历史记录仍保留。</p>${field("translation_channel", "原文类型", tr.channel || "transcript", {select: {transcript: "完整文案", on_screen: "屏显文字", historical_excerpt: "历史文字片段"}})}${field("translation_source_text", "对应原文", tr.source_text || t.text || "", {area: true, help: "完整文案类型自动采用上方文案。"})}${field("translation_text", "中文口语译文", tr.text || "", {area: true, script: true})}</details>`;
      case "insights": return `${field("highlights", "可借鉴的表达", (current.highlights || []).join("\n"), {area: true, help: "每行一条"})}${field("limitations", "问题与适用边界", (current.limitations || []).join("\n"), {area: true, help: "每行一条；原拆解来源资料保持原样。"})}`;
      case "direction": return window.CaseDirectionPicker.markup(current);
      case "release": return releaseEditor(current);
      case "metrics": return `<p class="context-note">销售数字仍从数据表读取。这里调整关联的视频与账号，原有表现快照保留。</p>${field("metric_video_id", "视频 ID", m.video_id || "")}${field("metric_creator_handle", "发布账号", m.creator_handle || "")}${field("metric_published_title", "发布标题", m.published_title || "")}`;
      case "assets": return videoEditorFields(current, true);
      case "native": return nativeEditor(current);
      default: throw new Error("这个分区暂不支持编辑。");
    }
  }
  function collect(editor, values) {
    const current = structuredClone(editor.current), value = name => String(values.get(name) ?? "").trim();
    const setOptional = (target, key, text) => { if (text) target[key] = text; else delete target[key]; };
    switch (editor.sectionId) {
      case "overview": {
        for (const key of ["title", "conclusion", "category", "kind"]) current[key] = value(key);
        if (!current.title || !current.conclusion) throw new Error("请填写案例标题和一句话结论。");
        for (const key of ["purchase_reason", "content_origin"]) setOptional(current, key, value(key));
        const retained = (current.assessments || []).filter(item => !["reach", "conversion"].includes(item.dimension));
        for (const dimension of ["reach", "conversion"]) {
          const old = current.assessments?.find(item => item.dimension === dimension), polarity = value("polarity_" + dimension) || "unknown", reason = value("reason_" + dimension);
          if (old || polarity !== "unknown" || reason) {
            const next = {...old, dimension, polarity}; setOptional(next, "reason", reason); retained.push(next);
          }
        }
        current.assessments = retained;
        break;
      }
      case "transcript": {
        if (value("transcript_text")) current.transcript = {...current.transcript, text: value("transcript_text"), status: value("transcript_status") || "用户提供，未逐字核对视频", source: value("transcript_source") || "用户在本地案例工作台填写"};
        else delete current.transcript;
        const prior = editor.current.translation_zh, channel = value("translation_channel"), source = channel === "transcript" ? current.transcript?.text || "" : value("translation_source_text"), text = value("translation_text");
        if (!text || (prior && text === prior.text && (source !== prior.source_text || channel !== prior.channel)) || (prior?.channel === "on_screen" && editor.current.transcript?.text?.includes(prior.source_text) && !current.transcript?.text?.includes(source))) delete current.translation_zh;
        else { if (!source) throw new Error("请先填写中文对照对应的原文。"); current.translation_zh = {source_text: source, text, channel}; }
        break;
      }
      case "insights": for (const key of ["highlights", "limitations"]) current[key] = lines(value(key)); break;
      case "direction": window.CaseDirectionPicker.collect(current, value, editor.form); break;
      case "release": {
        const publish = {...current.publish}, production = {...current.production};
        setOptional(publish, "published_on", value("publish_published_on"));
        for (const [key] of productionFields) setOptional(production, key, value("production_" + key));
        if (Object.keys(publish).length) current.publish = publish; else delete current.publish;
        if (Object.keys(production).length) current.production = production; else delete current.production;
        const days = daysSincePublish(current); if (days !== null && days < 0) throw new Error("数据截至日不能早于发布日期。");
        break;
      }
      case "metrics": {
        const metrics = {...current.metrics};
        for (const key of ["video_id", "creator_handle", "published_title"]) setOptional(metrics, key, value("metric_" + key));
        current.metrics = metrics; break;
      }
      case "assets": {
        const assets = {...current.assets};
        for (const kind of Object.keys(assetLabels).filter(kind => kind !== "screenshot")) setOptional(assets, kind, value("asset_" + kind));
        current.assets = assets; break;
      }
      case "native": collectNativeReference(current, value); break;
      default: throw new Error("这个分区暂不支持编辑。");
    }
    return {current, revision: editor.item.revision, note: value("note") || `更新${labels[editor.sectionId]}`};
  }
  function bindTranslation(current, form) {
    const prior = current.translation_zh || {}, get = name => $(`[name="${name}"]`, form);
    const sync = () => {
      const channel = get("translation_channel").value, source = get("translation_source_text"), translated = get("translation_text");
      source.readOnly = channel === "transcript";
      if (channel === "transcript") source.value = get("transcript_text").value;
      if (prior.text && translated.value.trim() === prior.text && (channel !== prior.channel || source.value.trim() !== prior.source_text)) translated.value = "";
    };
    get("translation_channel").addEventListener("change", sync);
    get("transcript_text").addEventListener("input", sync);
    get("translation_source_text").addEventListener("input", sync);
    sync();
  }
  function open(item, sectionId = "overview") {
    if (!item?.case_id || !labels[sectionId]) return false;
    if (state.editor || state.deletion || state.deleteOperation || state.bulk || state.bulkOperation || state.archiveOperation || window.CaseAI?.modalOpen()) {toast("请先保存或关闭当前编辑和操作。", true); return false;}
    if (!item.current) return openEditor(item, {advanced: true});
    window.CaseDetailTabs?.showSection(sectionId);
    const host = $(`[data-inline-section="${sectionId}"]`) || $(`#detail-section-${legacySections[sectionId]}`);
    if (!host) {toast("请先打开这条案例，再编辑对应分区。", true); return false;}
    const form = document.createElement("form"); form.className = "case-inline-editor"; form.dataset.editSection = sectionId;
    form.setAttribute("aria-label", `编辑${labels[sectionId]}`);
    form.innerHTML = `<div class="inline-editor-heading"><h3>编辑${labels[sectionId]}</h3><button type="button" class="subtle" data-inline-cancel>取消</button></div><div class="inline-editor-fields">${fields(sectionId, item)}</div><details class="advanced inline-revision-note"><summary>本次修改说明（可选）</summary>${field("note", "修改说明", "", {area: true, help: "留空会按所修改的分区记录。"})}</details><p class="inline-editor-status" data-inline-status role="status">保存会追加修订，其他分区保持原样。</p><div class="inline-editor-actions"><button type="button" class="subtle" data-inline-copy>复制未保存内容</button><button type="button" class="secondary" data-inline-cancel>取消</button><button type="submit" class="primary" data-inline-save>保存此分区</button></div>`;
    const editor = {mode: "inline", sectionId, form, host, item: structuredClone(item), current: structuredClone(item.current), originHash: location.hash, dirty: false, uploads: 0, videoUpload: null};
    state.editor = editor; host.append(form); host.classList.add("is-editing-inline");
    form.addEventListener("input", () => {editor.dirty = true;}); form.addEventListener("change", () => {editor.dirty = true;});
    form.addEventListener("submit", save);
    $$("[data-inline-cancel]", form).forEach(button => button.onclick = requestClose);
    $("[data-inline-copy]", form).onclick = () => {
      const rows = [...form.querySelectorAll("input[name],textarea[name],select[name]")].filter(control => !["file", "hidden"].includes(control.type)).map(control => `${control.closest(".field")?.querySelector("span")?.textContent || control.name}：${control.type === "checkbox" ? control.checked ? "是" : "否" : control.value}`);
      copyText(`${labels[sectionId]} · ${item.current?.title || item.title}\n\n${rows.join("\n\n")}`, "未保存内容已复制");
    };
    if (sectionId === "overview") {
      $('[name="category"]', form).addEventListener("change", renderCategoryFeedback);
      $('[name="polarity_conversion"]', form).addEventListener("change", renderCategoryFeedback);
      renderCategoryFeedback();
    }
    if (sectionId === "transcript") bindTranslation(item.current, form);
    if (sectionId === "direction") window.CaseDirectionPicker.bind(form, item);
    if (sectionId === "assets") {bindEditorUploads(form); renderVideoImportStatus();}
    if (sectionId === "native") bindNativeEditor();
    editor.discardClose = () => {if (state.editor === editor) {editor.pendingNavigation = null; editor.pendingForce = false;}};
    $("#discard-dialog").addEventListener("close", editor.discardClose);
    form.scrollIntoView({block: "nearest"}); $("input:not([type=hidden]),textarea,select", form)?.focus({preventScroll: true});
    return true;
  }
  async function save(event) {
    event?.preventDefault(); const editor = state.editor;
    if (editor?.mode !== "inline" || editor.deleted || editor.uploads || editor.saving) return;
    const status = $("[data-inline-status]", editor.form), button = $("[data-inline-save]", editor.form);
    let payload;
    try {payload = collect(editor, new FormData(editor.form));}
    catch (error) {status.textContent = error.message; status.classList.add("is-error"); return;}
    const fingerprint = JSON.stringify(payload);
    if (fingerprint !== editor.fingerprint) {editor.requestId = crypto.randomUUID(); editor.fingerprint = fingerprint;}
    payload.request_id = editor.requestId; editor.saving = true;
    const controls = $$("input,textarea,select,button", editor.form); controls.forEach(control => control.disabled = true);
    status.textContent = "正在保存…"; status.classList.remove("is-error");
    try {
      const result = await api(`/api/cases/${encodeURIComponent(editor.item.case_id)}`, {method: "POST", body: payload});
      if (state.editor !== editor) return;
      let warning = result.warning || "";
      const target = categoryView(payload.current.category);
      if (editor.sectionId === "overview" && !navigationAssignment(editor.item.case_id) && target && payload.current.category !== editor.current.category) {
        try {await api("/api/navigation/move", {method: "POST", body: {case_id: editor.item.case_id, target, revision: result.case.revision, previous: state.navigation[editor.item.case_id] ?? null}});}
        catch (error) {warning = `案例已保存，栏目归属暂未同步：${error.message}`;}
      }
      editor.dirty = false; editor.saving = false; state.pendingSync = false;
      close({refresh: false}); state.detail = result.case;
      renderDetail(result.case); updateNav();
      window.CaseDetailTabs?.showSection(editor.sectionId);
      toast(warning || (result.unchanged ? "没有变更，记录保持原样" : `${labels[editor.sectionId]}已保存`), !!warning);
      void refresh(true);
    } catch (error) {
      if (state.editor !== editor) return;
      status.textContent = error.status === 409 ? `${error.message} 未保存内容仍在，请复制或取消后读取最新版；不会覆盖其他位置的修改。` : error.message;
      status.classList.add("is-error");
      if (error.status === 404) handleDeleted(editor.item.case_id);
    } finally {
      editor.saving = false;
      if (state.editor === editor) {controls.forEach(control => control.disabled = false); button.disabled = !!editor.deleted; updateUploadControls(editor);}
    }
  }
  function close(options = {}) {
    const editor = state.editor; if (editor?.mode !== "inline") return;
    if (editor.uploads || editor.saving) {toast("正在复制或保存，请稍候。", true); return;}
    const destination = editor.pendingNavigation, force = editor.pendingForce;
    $("#discard-dialog").removeEventListener("close", editor.discardClose);
    state.editor = null; editor.host.classList.remove("is-editing-inline"); editor.form.remove();
    if ($("#discard-dialog").open) $("#discard-dialog").close();
    if (destination) {if (location.hash !== destination) location.hash = destination; else if (force) void route(true, true);}
    else if (options.refresh !== false && (state.pendingSync || editor.deleted)) {state.pendingSync = false; void route(true, true);}
  }
  function beforeNavigate(incoming, force = false) {
    const editor = state.editor; if (editor?.mode !== "inline") return true;
    const sameCase = incoming.page === "case" && incoming.tail?.[0] === editor.item.case_id;
    if (sameCase && !force) {editor.originHash = location.hash; return true;}
    if (!editor.dirty && !editor.uploads && !editor.saving && !editor.deleted) {close({refresh: false}); return true;}
    const destination = location.hash;
    if (location.hash !== editor.originHash) history.replaceState(null, "", editor.originHash);
    if (editor.uploads || editor.saving) {toast("正在复制或保存，请完成后再离开。", true); return false;}
    editor.pendingNavigation = destination; editor.pendingForce = force;
    requestClose(); return false;
  }
  function handleDeleted(caseId) {
    const editor = state.editor; if (editor?.mode !== "inline" || editor.item.case_id !== caseId) return false;
    editor.deleted = true;
    editor.uploadRequest?.abort();
    const status = $("[data-inline-status]", editor.form); status.textContent = "这条案例已在其他位置删除。未保存内容仍保留在这里，可以复制后关闭。"; status.classList.add("is-error");
    updateUploadControls(editor); return true;
  }
  return {open, save, close, beforeNavigate, handleDeleted};
})();
