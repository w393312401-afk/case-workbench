"use strict";

function bulkName(action) {
  return { archive: "批量归档", restore: "批量恢复", delete: "批量彻底删除" }[
    action
  ];
}
function bulkResultText(bulk) {
  const success = bulk.results.filter((result) => result.success).length,
    failed = bulk.results.length - success;
  return `${bulkName(bulk.action)}：成功 ${success} 条${failed ? `，未完成 ${failed} 条。仍在当前筛选中的失败项已保留选择，可核对后重试。` : "。"}`;
}
function updateBulkControls() {
  const bulk = state.bulk;
  if (!bulk) return;
  const busy = bulk.submitting,
    expected = `删除 ${bulk.items.length} 条案例`;
  $("#close-bulk").disabled = busy;
  $("#cancel-bulk").disabled = busy;
  $("#confirm-bulk").hidden = bulk.action !== "delete" || bulk.finished;
  $("#confirm-bulk").disabled =
    busy ||
    bulk.loading ||
    bulk.items.some((item) => !bulk.plans[item.case_id]) ||
    $("#bulk-confirmation")?.value.trim() !== expected;
  $("#confirm-bulk").textContent = busy ? "正在逐条删除…" : "彻底删除所选案例";
  if ($("#bulk-confirmation"))
    $("#bulk-confirmation").disabled = busy || bulk.loading;
  $("#bulk-hint").textContent = busy
    ? "正在逐条处理，请等待结果。"
    : bulk.finished
      ? "每条结果已列出；未完成项可重新选择并核对。"
      : "本次选择已固定，筛选和后续新增案例不会加入。";
  $("#bulk-form").setAttribute("aria-busy", String(busy || bulk.loading));
}
function renderBulkDialog() {
  const bulk = state.bulk;
  if (!bulk) return;
  $("#bulk-title").textContent =
    `${bulkName(bulk.action)} · ${bulk.items.length} 条`;
  const files = new Map();
  Object.values(bulk.plans).forEach((plan) =>
    (plan.files || []).forEach((file) => files.set(file.path, file)),
  );
  const bytes = [...files.values()].reduce(
    (sum, file) => sum + (Number(file.bytes) || 0),
    0,
  );
  const deletePreview = bulk.action === "delete" && !bulk.finished;
  $("#bulk-fields").innerHTML =
    `<div class="delete-content">${deletePreview ? `<div class="delete-warning"><div><strong>彻底删除所选案例及下列本地数据，无法恢复。</strong><p>此次保留旧库源文件、外部原素材和独立备份。共用文件是否清理以每条预览为准；执行时若删除范围扩大，将跳过该条并要求重新核对。</p></div></div><div class="delete-summary"><strong>已核对 ${Object.keys(bulk.plans).length} / ${bulk.items.length} 条</strong><span>${files.size} 个文件 · ${esc(readableBytes(bytes))}</span></div>` : ""}${bulk.loading ? '<p class="bulk-progress" role="status">正在逐条核对删除清单…</p>' : ""}${bulk.submitting ? `<p class="bulk-progress" role="status">正在处理 ${Math.min(bulk.results.length + 1, bulk.items.length)} / ${bulk.items.length}：${esc(bulk.activeTitle || "")}</p>` : ""}${bulk.finished ? `<p class="bulk-completion" role="status">${esc(bulkResultText(bulk))}</p>` : ""}<div class="bulk-items">${bulk.items
      .map((item) => {
        const plan = bulk.plans[item.case_id],
          result = bulk.results.find((entry) => entry.case_id === item.case_id),
          error = bulk.previewErrors[item.case_id];
        return `<section class="bulk-item"><h3>${esc(item.title)}</h3><p class="delete-case-id">${esc(item.case_id)}</p>${result ? `<p class="bulk-item-result ${result.success ? "success" : "failure"}">${esc(result.success ? "已完成" : "未完成")}：${esc(result.message)}</p>` : error ? `<p class="bulk-item-result failure">${esc(error)}</p>` : !deletePreview ? '<p class="bulk-item-result">等待处理</p>' : ""}${deletePreview && plan ? `<details class="delete-file-group"><summary>将删除 ${(plan.files || []).length} 个文件 · ${esc(readableBytes(plan.total_bytes))}</summary><ul class="delete-file-list">${plan.files.map((file) => `<li><div><span>${esc(deletionKind(file.kind))}</span><small>${esc(readableBytes(file.bytes))}</small></div><code>${esc(file.path)}</code></li>`).join("")}</ul></details>${plan.retained?.length ? `<details class="delete-file-group retained-files"><summary>保留 ${plan.retained.length} 个文件</summary><ul class="delete-file-list">${plan.retained.map((file) => `<li><code>${esc(file.path)}</code><p>${esc(file.reason)}</p></li>`).join("")}</ul></details>` : ""}${plan.warnings?.length ? `<ul class="bulk-preview-warnings">${plan.warnings.map((warning) => `<li>${esc(warning)}</li>`).join("")}</ul>` : ""}` : ""}</section>`;
      })
      .join(
        "",
      )}</div>${deletePreview && !bulk.loading ? (Object.keys(bulk.previewErrors).length ? '<p class="bulk-item-result failure">部分清单未通过核对，本次不能提交。请关闭并重新同步、选择案例后重试。</p>' : `<label class="field delete-confirmation"><span>请输入 <strong>删除 ${bulk.items.length} 条案例</strong> 确认</span><input id="bulk-confirmation" autocomplete="off" spellcheck="false" placeholder="删除 ${bulk.items.length} 条案例"></label>`) : ""}</div>`;
  $("#bulk-confirmation")?.addEventListener("input", updateBulkControls);
  updateBulkControls();
}
function validateBulkPlan(plan, item) {
  if (
    plan.case_id !== item.case_id ||
    !plan.plan_token ||
    !Array.isArray(plan.files) ||
    plan.source?.included
  )
    throw new Error("删除清单或来源范围不完整，请重新核对。");
  if (plan.revision !== item.revision)
    throw new Error("案例在选定后已更新，请重新同步并选择。");
}
function deletionScopeFits(confirmed, fresh) {
  const key = (file) =>
    JSON.stringify([
      String(file.path).replace(/\\/g, "/").toLowerCase(),
      file.kind,
      Number(file.bytes),
    ]);
  const approved = new Set(confirmed.files.map(key));
  return fresh.files.every((file) => approved.has(key(file)));
}
async function startBulk(action) {
  if (
    window.CaseAI?.modalOpen() ||
    state.bulk ||
    state.bulkOperation ||
    state.editor ||
    state.deletion ||
    state.archiveOperation ||
    state.deleteOperation
  ) {
    toast("请先完成或关闭当前编辑及操作。", true);
    return;
  }
  const items = filteredCases()
    .filter((item) => state.selection.has(item.case_id))
    .map((item) =>
      Object.freeze({
        case_id: item.case_id,
        revision: item.revision,
        archived: !!item.archived,
        title: item.current?.title || item.title || "未命名案例",
      }),
    );
  if (
    !items.length ||
    items.some((item) =>
      action === "archive" ? item.archived : !item.archived,
    )
  ) {
    toast("所选案例的状态不适用于这项操作，请重新选择。", true);
    return;
  }
  const bulk = {
    action,
    items: Object.freeze(items),
    plans: {},
    previewErrors: {},
    results: [],
    loading: action === "delete",
    submitting: false,
    finished: false,
    activeTitle: "",
  };
  state.bulk = bulk;
  state.lastBulkResult = "";
  renderBulkDialog();
  $("#bulk-dialog").showModal();
  renderSelectionToolbar();
  if (action !== "delete") {
    await executeBulk(bulk);
    return;
  }
  for (const item of bulk.items) {
    if (state.bulk !== bulk) return;
    try {
      const plan = await api(
        `/api/cases/${encodeURIComponent(item.case_id)}/delete-preview?include_source=false`,
      );
      if (state.bulk !== bulk) return;
      validateBulkPlan(plan, item);
      bulk.plans[item.case_id] = plan;
    } catch (error) {
      if (state.bulk !== bulk) return;
      bulk.previewErrors[item.case_id] = error.message;
    }
    renderBulkDialog();
  }
  bulk.loading = false;
  renderBulkDialog();
  $("#bulk-confirmation")?.focus();
}
async function executeBulk(bulk) {
  if (state.bulk !== bulk || bulk.submitting || bulk.finished || bulk.loading)
    return;
  bulk.submitting = true;
  state.bulkOperation = true;
  mutationSequence++;
  routeSequence++;
  for (const item of bulk.items) {
    bulk.activeTitle = item.title;
    renderBulkDialog();
    let submitted = false;
    try {
      let result;
      if (bulk.action === "delete") {
        const fresh = await api(
          `/api/cases/${encodeURIComponent(item.case_id)}/delete-preview?include_source=false`,
        );
        validateBulkPlan(fresh, item);
        if (!deletionScopeFits(bulk.plans[item.case_id], fresh))
          throw new Error(
            "本地文件的删除范围已变化，本次已跳过。请重新预览确认后再删除。",
          );
        submitted = true;
        result = await api(
          `/api/cases/${encodeURIComponent(item.case_id)}/delete`,
          {
            method: "POST",
            body: {
              request_id: crypto.randomUUID(),
              revision: item.revision,
              plan_token: fresh.plan_token,
              confirmation: item.case_id,
              include_source: false,
            },
          },
        );
        if (result.deleted !== true || result.case_id !== item.case_id)
          throw new Error(
            "未收到完整删除结果，请重新同步后核对；本次不会自动重试。",
          );
        state.cases = state.cases.filter(
          (entry) => entry.case_id !== item.case_id,
        );
      } else {
        const archived = bulk.action === "archive";
        submitted = true;
        result = await api(
          `/api/cases/${encodeURIComponent(item.case_id)}/archive`,
          {
            method: "POST",
            body: {
              request_id: crypto.randomUUID(),
              revision: item.revision,
              archived,
              note: archived ? "用户批量归档案例" : "用户批量恢复案例",
            },
          },
        );
        if (
          result.case?.case_id !== item.case_id ||
          result.case.archived !== archived
        )
          throw new Error(
            "未收到完整状态，请重新同步后核对；本次不会自动重试。",
          );
        state.cases = state.cases.map((entry) =>
          entry.case_id === item.case_id ? { ...entry, ...result.case } : entry,
        );
      }
      state.selection.delete(item.case_id);
      bulk.results.push({
        case_id: item.case_id,
        success: true,
        message:
          result.warning ||
          (bulk.action === "delete"
            ? "案例及已确认范围内的本地文件已删除。"
            : bulk.action === "archive"
              ? "已归档，原分类保留。"
              : "已恢复到原分类。"),
      });
    } catch (error) {
      const message =
        error.status === 409
          ? "案例或清单已更新，本次未完成。请重新同步并核对后重试。"
          : submitted && (!error.status || error.status >= 500)
            ? `未能确认结果：${error.message}。请重新同步核对，本次不会自动重试。`
            : error.message;
      bulk.results.push({ case_id: item.case_id, success: false, message });
    }
  }
  bulk.submitting = false;
  bulk.finished = true;
  state.bulkOperation = false;
  state.signature = "";
  state.digest = "";
  state.lastBulkResult = bulkResultText(bulk);
  renderBulkDialog();
  updateNav();
  await refresh(true);
}
function submitBulk(event) {
  event.preventDefault();
  const bulk = state.bulk;
  if (
    !bulk ||
    bulk.action !== "delete" ||
    bulk.submitting ||
    bulk.loading ||
    bulk.finished ||
    bulk.items.some((item) => !bulk.plans[item.case_id]) ||
    $("#bulk-confirmation")?.value.trim() !== `删除 ${bulk.items.length} 条案例`
  )
    return;
  void executeBulk(bulk);
}
function closeBulk() {
  if (state.bulkOperation || state.bulk?.submitting) {
    toast("正在逐条处理，请等待结果。", true);
    return;
  }
  state.bulk = null;
  $("#bulk-dialog").close();
  $("#bulk-fields").innerHTML = "";
  state.pendingSync = false;
  if (!location.hash || (window.WorkbenchShell?.legacyHash() || location.hash).startsWith("#library")) {
    updateNav();
    renderLibrary();
  } else void route(true);
}
