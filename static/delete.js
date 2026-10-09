"use strict";

function readableBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "大小未提供";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
function deletionKind(kind) {
  return (
    {
      record: "案例记录",
      case_record: "案例记录",
      original: "原始记录",
      event: "修订记录",
      history: "修订历史",
      reading: "阅读副本",
      reading_copy: "阅读副本",
      asset: "案例附件",
      evidence: "证据附件",
      video: "视频",
      screenshot: "截图",
      transcript: "文案文件",
      contact_sheet: "画面缩略图",
      source_manifest: "来源清单",
      source: "旧库源案例",
      source_case: "旧库源案例",
    }[kind] || "本地文件"
  );
}
function deletionMessage(message, kind = "error") {
  const box = $("#delete-message");
  if (!box) return;
  box.textContent = message || "";
  box.hidden = !message;
  box.className = `delete-message ${kind}`;
}
function updateDeleteControls() {
  const deletion = state.deletion;
  const submitting = !!deletion?.submitting;
  $("#close-delete").disabled = submitting;
  $("#cancel-delete").disabled = submitting;
  $("#confirm-delete").disabled =
    !deletion?.plan ||
    deletion.loading ||
    submitting ||
    deletion.uncertain ||
    $("#delete-confirmation")?.value.trim() !== deletion.caseId;
  $("#confirm-delete").innerHTML =
    icon("trash") + (submitting ? "正在彻底删除…" : "彻底删除");
  $("#delete-form").setAttribute(
    "aria-busy",
    String(submitting || !!deletion?.loading),
  );
  if ($("#delete-source"))
    $("#delete-source").disabled =
      submitting ||
      deletion.loading ||
      !deletion.plan?.source?.available ||
      !!deletion.plan?.source?.blocked_reason;
  if ($("#delete-confirmation"))
    $("#delete-confirmation").disabled =
      submitting || deletion.loading || deletion.uncertain;
  if ($("#reload-delete-preview"))
    $("#reload-delete-preview").disabled = submitting || deletion.loading;
  $("#delete-hint").textContent = submitting
    ? "正在删除本地数据，请等待结果。"
    : deletion?.uncertain
      ? "结果尚未确认；重新核对前不会再次发送删除请求。"
      : "输入完整案例编号后才能确认，删除后无法恢复。";
}
function renderDeleteDialog() {
  const deletion = state.deletion;
  if (!deletion) return;
  const plan = deletion.plan,
    files = Array.isArray(plan?.files) ? plan.files : [],
    retained = Array.isArray(plan?.retained) ? plan.retained : [];
  const warnings = Array.isArray(plan?.warnings) ? plan.warnings : [];
  const updates = Array.isArray(plan?.updates) ? plan.updates : [];
  const title = plan?.title || deletion.title;
  $("#delete-fields").innerHTML =
    `<div class="delete-content"><div class="delete-warning" id="delete-description">${icon("trash")}<div><strong>这次操作会删除本地数据，无法恢复。</strong><p>请核对下面列出的文件。清理工作台正式记录、历史、阅读页及不共用的托管附件；外部原素材、独立历史备份不会自动清理。</p></div></div><p class="delete-case-name">${esc(title)}</p><p class="delete-case-id">案例编号：<code>${esc(deletion.caseId)}</code></p><div class="delete-message error" id="delete-message" role="alert" hidden></div>${deletion.loading ? '<div class="delete-loading" role="status">正在核对本地文件和引用关系…</div>' : ""}${plan ? `<div class="delete-summary"><strong>${files.length} 个文件</strong><span>共 ${esc(readableBytes(plan.total_bytes))}</span></div><details class="delete-file-group" open><summary>将从本地删除</summary>${files.length ? `<ul class="delete-file-list">${files.map((file) => `<li><div><span>${esc(deletionKind(file.kind))}</span><small>${esc(readableBytes(file.bytes))}</small></div><code>${esc(file.path)}</code></li>`).join("")}</ul>` : '<p class="delete-note">当前清单没有可删除的文件。请重新核对后再操作。</p>'}</details>${retained.length ? `<details class="delete-file-group retained-files" open><summary>以下文件会保留 · ${retained.length} 个</summary><ul class="delete-file-list">${retained.map((file) => `<li><code>${esc(file.path)}</code><p>${esc(file.reason || "未列入本次删除范围")}</p></li>`).join("")}</ul></details>` : ""}${updates.length ? `<details class="delete-file-group updated-files"><summary>同步更新 · ${updates.length} 处</summary><ul class="delete-file-list">${updates.map((file) => `<li><span>${esc(["tombstone", "deletion_registry", "deletion_marker"].includes(file.kind) ? "防重复导入登记 · 仅保留来源编号" : "目录或索引")}</span><code>${esc(file.path)}</code></li>`).join("")}</ul><p class="delete-note">目录中会移除此案例；防重复导入登记只保留来源编号，不保留正文和附件。</p></details>` : ""}${plan.source?.available || plan.source?.blocked_reason ? `<label class="delete-source-choice"><input type="checkbox" id="delete-source" ${deletion.includeSource ? "checked" : ""}><span>同时删除旧库源案例${plan.source.label ? `<small>${esc(plan.source.label)}</small>` : ""}</span></label>${plan.source.blocked_reason ? `<p class="delete-note source-blocked-note">${esc(plan.source.blocked_reason)}</p>` : ""}<p class="delete-note source-delete-note">${plan.source.blocked_reason ? "可以取消本次操作，或仅清理工作台内的数据。" : deletion.includeSource ? "已纳入旧库源案例；删除时会同步更新旧库索引。下方编号需要重新输入。" : "目前保留旧库源案例。勾选后将重新核对删除清单，并同步更新旧库索引。"}</p>` : ""}${warnings.length ? `<div class="delete-plan-warnings"><strong>本次删除说明</strong><ul>${warnings.map((warning) => `<li>${esc(warning)}</li>`).join("")}</ul></div>` : ""}<label class="field delete-confirmation"><span>请输入完整案例编号 <strong>${esc(deletion.caseId)}</strong> 确认</span><input id="delete-confirmation" name="delete_confirmation" autocomplete="off" spellcheck="false" placeholder="输入上方完整编号" aria-describedby="delete-description"></label>` : ""}<button type="button" class="subtle" id="reload-delete-preview" ${deletion.loading ? "hidden" : ""}>${icon("refresh")}重新核对删除清单</button>${!plan && deletion.includeSource && !deletion.loading ? '<button type="button" class="secondary" id="preview-workbench-only">仅清理工作台，重新预览</button>' : ""}</div>`;
  if (plan?.related_creation_note)
    $(".delete-confirmation", $("#delete-fields"))?.insertAdjacentHTML(
      "beforebegin",
      `<p class="delete-note">${esc(plan.related_creation_note)}</p>`,
    );
  $("#delete-confirmation")?.addEventListener("input", updateDeleteControls);
  $("#delete-source")?.addEventListener("change", (event) => {
    deletion.includeSource = event.target.checked;
    loadDeletePreview(deletion);
  });
  $("#reload-delete-preview").onclick = () => loadDeletePreview(deletion);
  $("#preview-workbench-only")?.addEventListener("click", () => {
    deletion.includeSource = false;
    loadDeletePreview(deletion);
  });
  deletionMessage(deletion.error, deletion.uncertain ? "warning" : "error");
  updateDeleteControls();
}
async function loadDeletePreview(deletion, message = "") {
  if (state.deletion !== deletion || deletion.submitting) return;
  const request = ++deletion.sequence;
  deletion.loading = true;
  deletion.plan = null;
  deletion.error = message;
  deletion.uncertain = false;
  renderDeleteDialog();
  try {
    const plan = await api(
      `/api/cases/${encodeURIComponent(deletion.caseId)}/delete-preview?include_source=${deletion.includeSource}`,
    );
    if (state.deletion !== deletion || request !== deletion.sequence) return;
    if (
      plan.case_id !== deletion.caseId ||
      !plan.revision ||
      !plan.plan_token ||
      !Array.isArray(plan.files)
    )
      throw new Error("删除清单不完整，请重新核对。");
    if (!!plan.source?.included !== deletion.includeSource)
      throw new Error("旧库源案例的删除范围与所选范围不一致，请重新核对。");
    deletion.plan = plan;
    deletion.includeSource = !!plan.source?.included;
  } catch (error) {
    if (state.deletion !== deletion || request !== deletion.sequence) return;
    deletion.error =
      error.status === 404
        ? "这条案例已不存在。请关闭窗口并重新同步，核对最新状态。"
        : `无法准备删除清单：${error.message}`;
  } finally {
    if (state.deletion === deletion && request === deletion.sequence) {
      deletion.loading = false;
      renderDeleteDialog();
      if (deletion.plan && !deletion.error) $("#delete-confirmation")?.focus();
    }
  }
}
async function openDelete(item) {
  if (!item.archived) {
    toast("请先归档案例，再使用彻底删除。", true);
    return;
  }
  if (
    window.CaseAI?.modalOpen() ||
    state.archiveOperation ||
    state.deleteOperation ||
    state.deletion ||
    state.bulk
  )
    return;
  if (state.editor) {
    toast("请先保存或关闭编辑，再删除案例；编辑内容已保留。", true);
    return;
  }
  const deletion = {
    caseId: item.case_id,
    title: item.current?.title || item.title || "未命名案例",
    includeSource: false,
    plan: null,
    sequence: 0,
    loading: false,
    submitting: false,
    uncertain: false,
    error: "",
  };
  state.deletion = deletion;
  renderDeleteDialog();
  $("#delete-dialog").showModal();
  await loadDeletePreview(deletion);
}
function closeDelete() {
  if (state.deleteOperation || state.deletion?.submitting) {
    toast("正在删除本地数据，请等待结果。", true);
    return;
  }
  state.deletion = null;
  $("#delete-dialog").close();
  $("#delete-fields").innerHTML = "";
  if (state.pendingSync) {
    state.pendingSync = false;
    route(true);
  }
}
function finishDelete(deletion, message, warning = false) {
  state.cases = state.cases.filter((item) => item.case_id !== deletion.caseId);
  state.selected = null;
  state.detail = null;
  state.signature = "";
  state.digest = "";
  state.pendingSync = false;
  state.view = navigationViewForContent("archived");
  state.query = "";
  state.product = "";
  state.archiveCategory = "all";
  state.sourceType = "all";
  state.detailTab = "current";
  state.archiveRequest = null;
  state.archiveError = null;
  state.deletion = null;
  $("#delete-dialog").close();
  $("#delete-fields").innerHTML = "";
  location.hash = "#library";
  updateNav();
  renderLibrary();
  toast(message, warning);
}
async function checkUncertainDelete(deletion, error) {
  deletion.uncertain = true;
  try {
    const data = await api("/api/state");
    if (!Array.isArray(data.cases)) throw new Error("同步返回不完整");
    Object.assign(state, {
      cases: data.cases,
      paths: data.paths,
      token: data.token,
      signature: data.signature,
      digest: data.digest || "",
    });
    updateNav();
    online(true);
    if (!data.cases.some((item) => item.case_id === deletion.caseId)) {
      finishDelete(
        deletion,
        "案例已不在库中。删除请求未返回完整结果，请核对本地文件的清理情况。",
        true,
      );
      return;
    }
    deletion.error = `删除结果尚未确认：${error.message}。同步后案例仍在库中，请重新核对删除清单；本次不会自动重试。`;
  } catch {
    deletion.error = `删除结果尚未确认：${error.message}。目前无法读取最新状态，请恢复连接后重新核对；本次不会自动重试。`;
  }
}
async function submitDelete(event) {
  event.preventDefault();
  const deletion = state.deletion;
  if (
    !deletion ||
    !deletion.plan ||
    deletion.loading ||
    deletion.submitting ||
    deletion.uncertain ||
    state.archiveOperation ||
    state.editor ||
    $("#delete-confirmation")?.value.trim() !== deletion.caseId
  )
    return;
  const plan = deletion.plan;
  deletion.submitting = true;
  deletion.error = "";
  state.deleteOperation = { caseId: deletion.caseId };
  mutationSequence++;
  routeSequence++;
  updateDeleteControls();
  deletionMessage("");
  let conflict = false;
  try {
    const result = await api(
      `/api/cases/${encodeURIComponent(deletion.caseId)}/delete`,
      {
        method: "POST",
        body: {
          request_id: crypto.randomUUID(),
          revision: plan.revision,
          plan_token: plan.plan_token,
          confirmation: deletion.caseId,
          include_source: deletion.includeSource,
        },
      },
    );
    if (result.deleted !== true || result.case_id !== deletion.caseId)
      throw new Error("删除请求未返回完整结果");
    finishDelete(
      deletion,
      result.warning || "案例及清单内的本地文件已彻底删除。",
      !!result.warning,
    );
  } catch (error) {
    conflict = error.status === 409;
    if (conflict)
      deletion.error =
        "案例或文件清单已发生变化。已重新核对，请检查最新清单并重新输入编号确认。";
    else if (!error.status || error.status >= 500)
      await checkUncertainDelete(deletion, error);
    else {
      deletion.error = `删除未完成：${error.message}。请重新核对清单后再确认。`;
      deletion.plan = null;
    }
  } finally {
    deletion.submitting = false;
    state.deleteOperation = null;
    if (state.deletion === deletion) {
      if (conflict) await loadDeletePreview(deletion, deletion.error);
      else renderDeleteDialog();
    } else await refresh(true);
  }
}
