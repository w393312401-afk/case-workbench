"use strict";

// Preferences and file locations share the existing workbench APIs.
(() => {
  let renderSequence = 0;
  const panelStates = new WeakMap();
  let loginPoll = null;
  let dataPoll = null;
  const dataStates = new WeakMap();
  const modelStates = new WeakMap();
  const remoteStates = new WeakMap();
  const effortNames = { none: "不启用", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "极致" };
  const loginPollInterval = 2500;
  const loginPollTimeout = 5 * 60 * 1000;
  const locations = [
    ["records", "正式案例记录", "原始资料、当前版本与每次修订。"],
    ["directions", "卖点与方向", "卖点表、方向定义、视频关联与迁移记录。"],
    ["creation", "创作与素材", "采纳的脚本版本、摘录篮、评论主题和待写计划。"],
    ["evidence", "视频与证据附件", "已导入的视频、截图和转写文件。"],
    ["library", "中文阅读目录", "随案例保存同步更新的阅读文件。"],
  ];

  const locationStates = new WeakMap();
  function renderLocation([key, label, note]) {
    const path = typeof state.paths?.[key] === "string" ? state.paths[key] : "";
    return `<div class="settings-location" data-location="${key}"><div class="settings-location-copy"><h3>${label}</h3><p>${note}</p><code class="settings-path">${esc(path || "正在读取路径…")}</code></div><div class="settings-actions"><button type="button" class="subtle" data-settings-copy="${key}" ${path ? "" : "disabled"}>${icon("copy")}复制路径</button><button type="button" class="secondary compact" data-settings-open="${key}" ${path ? "" : "disabled"}>${icon("folder")}打开文件夹</button></div></div>`;
  }

  function paintLocations(panel, local) {
    for (const [key] of locations) {
      const row = panel.querySelector(`[data-location="${key}"]`);
      if (!row) continue;
      const path = typeof local.paths?.[key] === "string" ? local.paths[key].trim() : "";
      row.dataset.state = path ? "ready" : local.loading ? "loading" : local.error ? "failed" : "unconfigured";
      row.querySelector("code").textContent = path || (local.loading ? "正在读取路径…" : local.error ? "路径读取失败" : "未配置此位置");
      row.querySelectorAll("button").forEach(button => { button.disabled = !path; });
    }
    const error = panel.querySelector("[data-location-error]");
    error.hidden = !local.error;
    error.querySelector("span").textContent = local.error || "";
    error.querySelector("button").onclick = () => void loadLocations(panel, renderSequence);
    panel.querySelectorAll("[data-settings-copy]").forEach(button => {
      button.onclick = () => { const path = local.paths?.[button.dataset.settingsCopy]; if (path) copyText(path, "路径已复制"); };
    });
  }
  async function loadLocations(panel, sequence) {
    const local = {loading:true, paths:{...state.paths}, error:""};
    locationStates.set(panel, local); paintLocations(panel, local);
    try {
      const result = await api("/api/settings/paths");
      if (!result?.paths || typeof result.paths !== "object") throw new Error("目录信息返回不完整，请重试。");
      for (const [key] of locations) if (!Object.hasOwn(result.paths, key)) throw new Error("目录信息返回不完整，请重试。");
      if (locations.some(([key]) => result.paths[key] != null && typeof result.paths[key] !== "string")) throw new Error("目录信息格式不正确，请重试。");
      local.paths = {...result.paths};
    } catch (error) {
      const complete = locations.every(([key]) => typeof state.paths?.[key] === "string" && state.paths[key].trim());
      local.paths = complete ? {...state.paths} : {};
      local.error = complete ? "" : `目录读取失败：${error.message || "请重试"}`;
    }
    if (!panel.isConnected || sequence !== renderSequence || locationStates.get(panel) !== local) return;
    local.loading = false;
    // No full-page rerender: other forms can contain unsaved input.
    paintLocations(panel, local);
  }

  function localAccountSetup() {
    return ["localhost", "127.0.0.1", "[::1]", "::1"].includes(window.location.hostname);
  }

  function renderRemoteAccount() {
    const heading = `<div class="settings-card-heading"><span class="settings-symbol">${icon("lock")}</span><h2 id="settings-remote-heading">公网账号密码</h2></div>`;
    if (!localAccountSetup()) return `<section class="section-card settings-card" aria-labelledby="settings-remote-heading">${heading}<p class="settings-note">账号密码在运行工作台的电脑上设置。</p></section>`;
    return `<section class="section-card settings-card" aria-labelledby="settings-remote-heading">${heading}<p class="settings-note">设置用于公网访问的账号密码。后续可在这里更改；更改后，已登录的设备需要重新登录。</p><p id="settings-remote-status" class="settings-note" role="status" aria-live="polite">正在读取账号状态…</p><form id="settings-remote-form"><div class="settings-remote-grid"><label for="settings-remote-username">公网登录账号<input id="settings-remote-username" name="username" type="text" autocomplete="username" minlength="3" maxlength="64" value="admin" required disabled></label><label for="settings-remote-password">新密码<input id="settings-remote-password" name="password" type="password" autocomplete="new-password" minlength="12" maxlength="256" aria-describedby="settings-remote-help" required disabled></label><label for="settings-remote-confirm">确认新密码<input id="settings-remote-confirm" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required disabled></label></div><p id="settings-remote-help" class="settings-note">密码至少 12 个字符，最多 256 个 UTF-8 字节。密码只在本机设置，不会显示在页面或聊天中。</p><div class="settings-actions"><button id="settings-remote-save" type="submit" class="primary compact" disabled>保存账号密码</button><button id="settings-remote-refresh" type="button" class="subtle">重新读取账号状态</button></div></form><p id="settings-remote-error" class="settings-note settings-claude-error" role="alert" hidden></p></section>`;
  }

  function paintRemoteAccount(panel) {
    const local = remoteStates.get(panel);
    if (!local) return;
    const disabled = local.loading || local.saving || !local.metadata?.remote_configured;
    $$("#settings-remote-form input", panel).forEach(input => { input.disabled = disabled; });
    $("#settings-remote-save", panel).disabled = disabled;
    $("#settings-remote-save", panel).textContent = local.saving ? "正在保存…" : "保存账号密码";
    $("#settings-remote-refresh", panel).disabled = local.loading || local.saving;
    const publicReady = local.metadata?.public_ready === true, guestEnabled = local.metadata?.guest_enabled === true;
    const accountStatus = publicReady
      ? local.metadata?.configured
        ? `已设置公网账号：${local.metadata.username}。${guestEnabled ? "访客无需登录即可浏览，登录后可使用完整功能。" : "登录后可使用完整功能。"}`
        : `${guestEnabled ? "访客浏览已开放；" : "尚未设置公网登录账号。"}设置账号密码后，可登录使用完整功能。`
      : local.metadata?.configured
        ? `本机已设置账号：${local.metadata.username}。${local.metadata.auth_mode === "password" ? "账号密码登录已在本机就绪；公网入口是否已切换需另行确认。" : "当前公网入口仍使用 Cloudflare 登录。"}`
        : "尚未设置公网登录账号。填写并保存后，本机账号密码即可就绪；Cloudflare 公网入口仍需切换。";
    $("#settings-remote-status", panel).textContent = local.loading ? "正在读取账号状态…" : local.notice || accountStatus;
    const error = $("#settings-remote-error", panel);
    error.textContent = local.error || "";
    error.hidden = !error.textContent;
  }

  async function loadRemoteAccount(panel) {
    const local = remoteStates.get(panel), sequence = renderSequence;
    if (!local || local.loading || local.saving) return;
    local.loading = true;
    local.error = "";
    paintRemoteAccount(panel);
    try {
      const result = await api("/api/remote-account");
      if (!panel.isConnected || sequence !== renderSequence) return;
      if (!result || typeof result.configured !== "boolean" || typeof result.remote_configured !== "boolean") throw new Error("账号状态暂时无法读取，请重试。");
      local.metadata = result;
      local.notice = "";
      local.error = result.error || "";
      $("#settings-remote-username", panel).value = result.username || "admin";
    } catch (error) {
      if (panel.isConnected && sequence === renderSequence) local.error = error.message || "账号状态暂时无法读取，请重试。";
    } finally {
      local.loading = false;
      if (panel.isConnected && sequence === renderSequence) paintRemoteAccount(panel);
    }
  }

  async function saveRemoteAccount(panel) {
    const local = remoteStates.get(panel), sequence = renderSequence;
    if (!local || local.loading || local.saving || !local.metadata?.remote_configured) return;
    const username = $("#settings-remote-username", panel).value.trim();
    const password = $("#settings-remote-password", panel).value;
    const confirmation = $("#settings-remote-confirm", panel).value;
    local.error = "";
    if (Array.from(username).length < 3 || Array.from(username).length > 64 || /[\p{C}]/u.test(username)) local.error = "账号名称须为 3–64 个字符，且不能包含控制字符。";
    else if (Array.from(password).length < 12 || new TextEncoder().encode(password).length > 256) local.error = "密码至少 12 个字符，最多 256 个 UTF-8 字节。";
    else if (password !== confirmation) local.error = "两次输入的密码不一致。";
    if (local.error) { paintRemoteAccount(panel); return; }
    local.saving = true;
    local.notice = "";
    paintRemoteAccount(panel);
    try {
      const result = await api("/api/remote-account", { method: "POST", body: { username, password, confirm_password: confirmation } });
      if (!result?.saved || !result.configured) throw new Error("保存结果暂时无法确认，请重新读取账号状态。");
      // Clear even if navigation detached this panel during the request.
      $("#settings-remote-password", panel).value = "";
      $("#settings-remote-confirm", panel).value = "";
      if (!panel.isConnected || sequence !== renderSequence) return;
      local.metadata = result;
      $("#settings-remote-username", panel).value = result.username;
      local.notice = result.notice || (result.public_ready === true
        ? "公网账号已更新，已登录的设备需要重新登录；访客浏览不受影响。"
        : "本机账号密码已就绪；Cloudflare 公网入口仍需切换。");
    } catch (error) {
      if (panel.isConnected && sequence === renderSequence) local.error = error.message || "保存失败，请重试。";
    } finally {
      local.saving = false;
      if (panel.isConnected && sequence === renderSequence) paintRemoteAccount(panel);
    }
  }

  function bindRemoteAccount(panel) {
    if (!localAccountSetup()) return;
    remoteStates.set(panel, { loading: false, saving: false, metadata: null, notice: "", error: "" });
    $("#settings-remote-form", panel).onsubmit = event => { event.preventDefault(); void saveRemoteAccount(panel); };
    $("#settings-remote-refresh", panel).onclick = () => { void loadRemoteAccount(panel); };
    void loadRemoteAccount(panel);
  }

  function modelState(panel) {
    if (!modelStates.has(panel)) modelStates.set(panel, { models: [], saved: null, draft: null, configured: false, loading: false, saving: false, error: "", notice: "", catalogError: "" });
    return modelStates.get(panel);
  }

  function modelIssue(local) {
    if (!local.draft) return "";
    const model = local.models.find(item => item.id === local.draft.model);
    if (local.draft.model && !model) return "已保存的模型当前不在可用列表中，请刷新模型列表或选择其他模型。";
    if (model?.available === false) return "所选模型尚未就绪，请检查对应客户端的连接，或选择其他模型。";
    if (local.draft.reasoning_effort && !(model?.reasoning_efforts || []).includes(local.draft.reasoning_effort)) return "当前模型不支持已选的思考强度，请重新选择。";
    return "";
  }

  function paintModelSettings(panel) {
    const local = modelState(panel), draft = local.draft || { model: null, reasoning_effort: null };
    const model = local.models.find(item => item.id === draft.model);
    const select = $("#settings-default-model", panel), effort = $("#settings-default-effort", panel);
    const provider = item => item.provider || (item.id.startsWith("claude-") ? "claude" : "codex");
    const option = item => `<option value="${esc(item.id)}" ${draft.model === item.id ? "selected" : ""} ${item.available === false ? "disabled" : ""}>${esc(item.name || item.id)}${item.available === false ? " · 未就绪" : ""}</option>`;
    select.innerHTML = `<option value="">${local.loading && !local.saved ? "正在读取模型…" : "本机自动选择"}</option>`
      + [["claude", "Claude"], ["codex", "Codex"]].map(([key, label]) => {
        const models = local.models.filter(item => provider(item) === key);
        return models.length ? `<optgroup label="${label}">${models.map(option).join("")}</optgroup>` : "";
      }).join("")
      + (draft.model && !model ? `<option value="${esc(draft.model)}" selected disabled>当前不可选 · ${esc(draft.model)}</option>` : "");
    const efforts = (model?.reasoning_efforts || []).filter(value => typeof value === "string");
    effort.innerHTML = `<option value="">${esc(model?.default_reasoning_effort ? `默认（${effortNames[model.default_reasoning_effort] || model.default_reasoning_effort}）` : "客户端默认")}</option>`
      + efforts.map(value => `<option value="${esc(value)}" ${draft.reasoning_effort === value ? "selected" : ""}>${esc(effortNames[value] || value)}</option>`).join("")
      + (draft.reasoning_effort && !efforts.includes(draft.reasoning_effort) ? `<option value="${esc(draft.reasoning_effort)}" selected disabled>当前不支持 · ${esc(draft.reasoning_effort)}</option>` : "");
    select.disabled = local.loading || local.saving || !local.saved;
    effort.disabled = select.disabled || !efforts.length;
    select.setAttribute("aria-busy", String(local.loading));
    const dirty = local.saved && JSON.stringify(local.saved) !== JSON.stringify(draft);
    const issue = modelIssue(local), save = $("#settings-default-save", panel);
    save.disabled = !local.saved || (!dirty && local.configured) || local.loading || local.saving || !!issue;
    save.textContent = local.saving ? "正在保存…" : "保存全局默认";
    $("#settings-default-refresh", panel).disabled = local.loading || local.saving;
    const status = $("#settings-default-status", panel);
    status.textContent = local.loading ? "正在读取全局默认与可用模型…" : local.notice || (dirty ? "有未保存的更改。" : local.saved ? local.configured ? "已保存的默认设置会用于下一次任务和继续对话。" : "请选择并保存全局默认，之后所有产品共用。" : "");
    const error = $("#settings-default-error", panel);
    error.textContent = local.error || issue;
    error.hidden = !error.textContent;
    const description = $("#settings-default-description", panel);
    description.textContent = local.catalogError || model?.description || (draft.model ? "选择的模型与思考强度会应用到接下来发起的任务。" : "本机自动选择：通用任务优先使用 Codex，Codex 未连接时使用已连接的 Claude。专用流程使用其支持的客户端。");
  }

  async function loadModelSettings(panel, refresh = false) {
    const local = modelState(panel), sequence = renderSequence;
    if (local.loading || local.saving) return;
    local.loading = true;
    local.error = "";
    paintModelSettings(panel);
    try {
      const catalog = await api(`/api/ai/options${refresh ? "?refresh=1" : ""}`);
      if (!panel.isConnected || sequence !== renderSequence) return;
      if (!catalog?.default_settings || !Array.isArray(catalog?.models)) throw new Error("全局模型设置暂时无法读取，请重试。");
      const dirty = local.saved && JSON.stringify(local.saved) !== JSON.stringify(local.draft);
      local.saved = { model: catalog.default_settings.model || null, reasoning_effort: catalog.default_settings.reasoning_effort || null };
      local.configured = catalog.defaults_configured !== false && !catalog.defaults_error;
      if (!dirty) local.draft = { ...local.saved };
      local.models = catalog.models.filter(item => item && typeof item.id === "string");
      local.catalogError = catalog.models_error || "";
      local.error = catalog.defaults_error || "";
      local.notice = "";
      if (!catalog.defaults_error) window.CaseAI?.refreshDefaults?.(local.saved);
    } catch (error) {
      if (panel.isConnected && sequence === renderSequence) local.error = error.message || "全局模型设置暂时无法读取，请重试。";
    } finally {
      local.loading = false;
      if (panel.isConnected && sequence === renderSequence) paintModelSettings(panel);
    }
  }

  async function saveModelSettings(panel) {
    const local = modelState(panel), sequence = renderSequence;
    if (!local.saved || local.loading || local.saving || modelIssue(local)) return;
    local.saving = true;
    local.error = "";
    local.notice = "";
    paintModelSettings(panel);
    try {
      const result = await api("/api/ai/preferences", { method: "POST", body: { ...local.draft } });
      if (!result?.preferences) throw new Error("保存结果暂时无法确认，请刷新后检查。");
      window.CaseAI?.refreshDefaults?.(result.preferences);
      if (!panel.isConnected || sequence !== renderSequence) return;
      local.saved = { model: result.preferences.model || null, reasoning_effort: result.preferences.reasoning_effort || null };
      local.draft = { ...local.saved };
      local.configured = true;
      local.notice = "全局默认已保存，下一次任务和继续对话将使用此设置。";
    } catch (error) {
      if (panel.isConnected && sequence === renderSequence) local.error = error.message || "保存失败，请重试。";
    } finally {
      local.saving = false;
      if (panel.isConnected && sequence === renderSequence) paintModelSettings(panel);
    }
  }

  function renderModelSettings() {
    return `<section class="section-card settings-card settings-model-card" aria-labelledby="settings-model-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("bot")}</span><h2 id="settings-model-heading">全局默认模型</h2></div><p class="settings-note">写稿、审稿、复盘等通用 AI 任务默认沿用这里的模型与思考强度，所有产品共用。剪辑与找底图也默认沿用，仅在不兼容时显示本次专用选项；配音保留独立配置。</p><form id="settings-default-form"><div class="settings-model-grid"><label for="settings-default-model">默认模型<select id="settings-default-model" disabled><option>正在读取模型…</option></select></label><label for="settings-default-effort">思考强度<select id="settings-default-effort" disabled><option>客户端默认</option></select></label></div><p class="settings-note" id="settings-default-description"></p><div class="settings-actions"><button type="submit" class="primary compact" id="settings-default-save" disabled>保存全局默认</button><button type="button" class="secondary compact" id="settings-default-refresh">刷新模型列表</button></div><p class="settings-note" id="settings-default-status" role="status" aria-live="polite"></p><p class="settings-note settings-claude-error" id="settings-default-error" role="alert" hidden></p></form></section>`;
  }

  function bindModelSettings(panel) {
    $("#settings-default-model", panel).onchange = event => {
      const local = modelState(panel);
      local.draft = { model: event.target.value || null, reasoning_effort: null };
      local.error = ""; local.notice = "";
      paintModelSettings(panel);
    };
    $("#settings-default-effort", panel).onchange = event => {
      const local = modelState(panel);
      local.draft.reasoning_effort = event.target.value || null;
      local.error = ""; local.notice = "";
      paintModelSettings(panel);
    };
    $("#settings-default-form", panel).onsubmit = event => { event.preventDefault(); void saveModelSettings(panel); };
    $("#settings-default-refresh", panel).onclick = () => loadModelSettings(panel, true);
    void loadModelSettings(panel);
  }

  function panelState(panel) {
    if (!panelStates.has(panel)) panelStates.set(panel, { claude: null, login: null, checking: false, loginStarting: false, loginAction: null, loginError: "", hadAttempt: false, attemptId: null, authRevision: 0 });
    return panelStates.get(panel);
  }

  function updateLoginButton(panel) {
    const button = $("#settings-claude-login", panel);
    if (!button) return;
    const local = panelState(panel);
    button.hidden = !!local.claude?.available;
    button.disabled = local.checking || local.loginStarting || !!local.loginAction || !!local.claude?.login_pending;
    button.textContent = local.loginStarting || local.claude?.login_pending ? "等待账号授权…" : local.hadAttempt ? "重新授权" : "连接 Claude 订阅";
    renderLoginControls(panel);
  }

  function officialAuthorizationUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const url = new URL(value);
      return url.protocol === "https:" && !url.username && !url.password && !url.port
        && ["claude.com", "claude.ai", "platform.claude.com", "console.anthropic.com"].includes(url.hostname)
        ? url.href : null;
    } catch { return null; }
  }

  function renderLoginControls(panel) {
    const local = panelState(panel);
    const recovery = $("#settings-claude-recovery", panel);
    if (!recovery) return;
    const pending = !!local.claude?.login_pending && !local.claude?.available;
    recovery.hidden = !pending;
    const url = pending ? officialAuthorizationUrl(local.login?.url) : null;
    const busy = local.loginStarting || !!local.loginAction;
    const canSubmit = pending && !!local.login?.attempt_id && !local.login?.callback_submitted && !busy;
    const link = $("#settings-claude-reopen", panel);
    link.hidden = !url;
    if (url) link.href = url;
    else link.removeAttribute("href");
    $("#settings-claude-copy", panel).disabled = !url;
    $("#settings-claude-url-note", panel).textContent = url
      ? "关闭授权窗口后，可重新打开或复制链接到其他浏览器继续。"
      : "正在获取授权链接；稍后可点「检查 Claude」重新读取。";
    const input = $("#settings-claude-callback", panel);
    input.disabled = !canSubmit;
    $("#settings-claude-submit", panel).disabled = !canSubmit || !input.value.trim();
    $("#settings-claude-submit", panel).textContent = local.loginAction === "callback" ? "正在提交…" : local.login?.callback_submitted ? "已提交，等待连接…" : "提交回调";
    $("#settings-claude-cancel", panel).disabled = !pending || !local.login?.attempt_id || busy;
    $("#settings-claude-cancel", panel).textContent = local.loginAction === "cancel" ? "正在取消…" : "取消本次授权";
    const error = $("#settings-claude-login-error", panel);
    const message = typeof local.login?.callback_error === "string" && local.login.callback_error ? local.login.callback_error : local.loginError;
    error.hidden = !message;
    error.textContent = message;
  }

  function applyLoginResult(panel, result) {
    if (!result) return;
    const local = panelState(panel);
    if (result.login) {
      if (result.login.attempt_id && local.attemptId !== result.login.attempt_id) {
        $("#settings-claude-callback", panel).value = "";
        local.loginError = "";
        local.attemptId = result.login.attempt_id;
      }
      local.login = result.login;
      local.hadAttempt ||= !!result.login.attempt_id;
    }
    if (result.claude) renderClaude(panel, result.claude);
    else renderLoginControls(panel);
  }

  async function recoverClaudeLogin(panel, { signal } = {}) {
    const sequence = renderSequence;
    const revision = panelState(panel).authRevision;
    const result = await api("/api/ai/claude/login", { headers: { "X-Workbench-Token": state.token }, ...(signal ? { signal } : {}) });
    if (panel.isConnected && sequence === renderSequence && revision === panelState(panel).authRevision && !signal?.aborted) {
      if (panelState(panel).loginError === "授权进度暂时无法读取，请点「检查 Claude」重试。") panelState(panel).loginError = "";
      applyLoginResult(panel, result);
    }
  }

  function stopLoginPolling() {
    if (!loginPoll) return;
    clearTimeout(loginPoll.timer);
    loginPoll.controller?.abort();
    loginPoll = null;
  }

  function continueLoginPolling(panel) {
    if (!panel.isConnected || !panelState(panel).claude?.login_pending) {
      if (loginPoll?.panel === panel) stopLoginPolling();
      return;
    }
    if (loginPoll && loginPoll.panel !== panel) stopLoginPolling();
    const expiry = Date.parse(panelState(panel).login?.expires_at);
    if (!loginPoll) loginPoll = { panel, deadline: Number.isFinite(expiry) ? expiry : Date.now() + loginPollTimeout, timer: null, controller: null };
    else if (Number.isFinite(expiry)) loginPoll.deadline = expiry;
    const current = loginPoll;
    if (current.timer !== null || current.controller) return;
    if (Date.now() >= current.deadline) {
      stopLoginPolling();
      $("#settings-claude-detail", panel).textContent = "等待授权已暂停。点击「检查 Claude」确认结果；如果已过期，可重新授权。";
      return;
    }
    current.timer = setTimeout(async () => {
      current.timer = null;
      if (loginPoll !== current || !panel.isConnected) { stopLoginPolling(); return; }
      current.controller = new AbortController();
      await checkCodex(panel, { signal: current.controller.signal, silent: true });
      current.controller = null;
      if (loginPoll === current) continueLoginPolling(panel);
    }, Math.min(loginPollInterval, current.deadline - Date.now()));
  }

  async function checkCodex(panel, { signal, silent = false } = {}) {
    const sequence = renderSequence;
    const local = panelState(panel);
    const revision = local.authRevision;
    if (local.checking) return;
    local.checking = true;
    const buttons = [$("#settings-check-codex", panel), $("#settings-claude-check", panel)].filter(Boolean);
    const status = $("#settings-codex-status", panel);
    const detail = $("#settings-codex-detail", panel);
    buttons.forEach(button => { button.disabled = true; });
    updateLoginButton(panel);
    if (!silent) {
      status.textContent = "正在检查…";
      status.dataset.status = "loading";
      detail.textContent = "";
    }
    const claudeStatus = $("#settings-claude-status", panel);
    if (claudeStatus && !silent) { claudeStatus.textContent = "正在检查…"; claudeStatus.dataset.status = "loading"; }
    try {
      const data = await api("/api/ai/status", signal ? { signal } : {});
      if (!panel.isConnected || sequence !== renderSequence || signal?.aborted) return;
      if (revision === local.authRevision) window.CaseAI?.refreshAvailability?.(data);
      const available = data.available && !data.shutting_down;
      const codex = data.providers?.codex || data;
      if (revision === local.authRevision) renderClaude(panel, data.providers?.claude || null);
      status.textContent = data.shutting_down ? "服务正在关闭" : codex.available ? "已找到本机 Codex" : available ? "未找到本机 Codex（可改用 Claude）" : "暂不可用";
      status.dataset.status = codex.available && !data.shutting_down ? "available" : "unavailable";
      const tasks = [data.running ? `${data.running} 项正在处理` : "", data.queued ? `${data.queued} 项排队中` : ""].filter(Boolean);
      detail.textContent = [codex.error || codex.label || data.message || "", ...tasks].filter(Boolean).join(" · ");
      if (revision === local.authRevision && !local.claude?.available) {
        try { await recoverClaudeLogin(panel, { signal }); }
        catch (error) {
          if (panel.isConnected && sequence === renderSequence && revision === local.authRevision && error.name !== "AbortError" && local.claude?.login_pending) {
            local.loginError = "授权进度暂时无法读取，请点「检查 Claude」重试。";
            renderLoginControls(panel);
          }
        }
      }
    } catch (error) {
      if (!panel.isConnected || sequence !== renderSequence || error.name === "AbortError") return;
      status.textContent = "状态暂时无法读取";
      if (!silent && revision === local.authRevision) renderClaude(panel, local.claude?.login_pending ? local.claude : null);
      status.dataset.status = "unavailable";
      detail.textContent = error.message || "请稍后重新检查。";
    } finally {
      local.checking = false;
      if (panel.isConnected) {
        buttons.forEach(button => { button.disabled = false; });
        updateLoginButton(panel);
      }
    }
  }

  function renderClaude(panel, claude) {
    const status = $("#settings-claude-status", panel), detail = $("#settings-claude-detail", panel);
    if (!status || !detail) return;
    panelState(panel).claude = claude;
    const ready = !!claude?.available;
    const pending = !!claude?.login_pending && !ready;
    status.textContent = !claude ? "Claude 状态暂未读取" : ready ? "已连接 Claude 订阅" : pending ? "正在等待 Claude 账号授权" : "Claude 订阅尚未连接";
    status.dataset.status = ready ? "available" : pending ? "loading" : "unavailable";
    const auth = ready && ["claude.ai", "oauth", "claude_ai", "subscription"].includes(claude.auth_method) ? "Claude 账号登录" : "";
    const subscription = ready && claude.subscription_type ? `订阅：${claude.subscription_type}` : "";
    detail.textContent = !claude ? "点击「连接 Claude 订阅」完成一次账号授权。" : [pending ? "使用与 Claude 桌面版相同的账号完成浏览器授权。页面会自动检查连接结果。" : claude.error || claude.label || (ready ? "使用客户端额度" : "点击「连接 Claude 订阅」完成一次账号授权。"), auth, subscription].filter(Boolean).join(" · ");
    if (ready || !pending) {
      panelState(panel).login = null;
      if (ready) $("#settings-claude-callback", panel).value = "";
      panelState(panel).loginError = "";
    }
    updateLoginButton(panel);
    if (ready || !pending) {
      if (loginPoll?.panel === panel) stopLoginPolling();
    } else continueLoginPolling(panel);
  }

  async function connectClaude(panel) {
    const sequence = renderSequence;
    const local = panelState(panel);
    if (local.checking || local.loginStarting || local.claude?.login_pending || local.claude?.available) return;
    local.loginStarting = true;
    ++local.authRevision;
    local.loginError = "";
    local.hadAttempt = true;
    updateLoginButton(panel);
    try {
      const result = await api("/api/ai/claude/login", { method: "POST", body: {} });
      if (!panel.isConnected || sequence !== renderSequence) return;
      ++local.authRevision;
      applyLoginResult(panel, result);
      if (result.message) toast(result.message);
      await checkCodex(panel, { silent: true });
    } catch (error) {
      if (!panel.isConnected || sequence !== renderSequence) return;
      $("#settings-claude-detail", panel).textContent = error.message || "连接暂时未能开始，请重试。";
    } finally {
      local.loginStarting = false;
      if (panel.isConnected) updateLoginButton(panel);
    }
  }

  async function submitClaudeLoginAction(panel, action) {
    const sequence = renderSequence;
    const local = panelState(panel);
    const attempt = local.login?.attempt_id;
    if (local.loginAction || !attempt || !local.claude?.login_pending) return;
    const input = $("#settings-claude-callback", panel);
    if (action === "callback" && (!input.value.trim() || local.login?.callback_submitted)) return;
    const body = { attempt_id: attempt };
    if (action === "callback") body.callback_url = input.value.trim();
    local.loginAction = action;
    ++local.authRevision;
    local.loginError = "";
    updateLoginButton(panel);
    try {
      const result = await api(action === "callback" ? "/api/ai/claude/callback" : "/api/ai/claude/login/cancel", { method: "POST", body });
      if (!panel.isConnected || sequence !== renderSequence) return;
      ++local.authRevision;
      if (action === "cancel") input.value = "";
      applyLoginResult(panel, result);
      if (result.message) toast(result.message);
      await checkCodex(panel, { silent: true });
    } catch (error) {
      if (!panel.isConnected || sequence !== renderSequence) return;
      local.loginError = error.message || "操作暂未完成，请重试。";
    } finally {
      local.loginAction = null;
      if (panel.isConnected) updateLoginButton(panel);
    }
  }

  // Pairing for the TK 看板 extension, which pushes public TikTok page counts into the sales table.
  async function loadExtensionLink(panel, sequence) {
    const target = $("#settings-extension-link", panel);
    const show = (link) => {
      target.innerHTML = `<div class="settings-source"><span class="settings-note">工作台地址</span><code class="settings-path">${esc(link.address)}</code><button type="button" class="subtle" data-extension-copy="address">${icon("copy")}复制</button></div><div class="settings-source"><span class="settings-note">连接码</span><code class="settings-path">${esc(link.key.slice(0, 4))}••••••••</code><button type="button" class="subtle" data-extension-copy="key">${icon("copy")}复制</button><button type="button" class="subtle" data-extension-rotate>重置</button></div>`;
      $$("[data-extension-copy]", target).forEach(button => {
        button.onclick = () => copyText(link[button.dataset.extensionCopy], button.dataset.extensionCopy === "key" ? "连接码已复制" : "地址已复制");
      });
      $("[data-extension-rotate]", target).onclick = async (event) => {
        if (!window.confirm("重置后，插件里保存的旧连接码会失效，需要重新粘贴。继续？")) return;
        event.currentTarget.disabled = true;
        try {
          const result = await api("/api/public-stats/link/rotate", { method: "POST", body: {} });
          show({ ...link, key: result.key });
          toast("连接码已重置，请到插件设置里粘贴新的连接码。");
        } catch (error) {
          toast(error.message, true);
          event.currentTarget.disabled = false;
        }
      };
    };
    try {
      const link = await api("/api/public-stats/link");
      if (sequence !== renderSequence || !panel.isConnected) return;
      show(link);
    } catch (error) {
      if (sequence !== renderSequence || !panel.isConnected) return;
      target.innerHTML = `<p class="settings-note">连接码暂时无法读取：${esc(error.message || "请稍后重试。")}</p>`;
    }
  }

  async function loadSources(panel, sequence) {
    const target = $("#settings-data-sources", panel);
    try {
      const data = await api("/api/performance/summary");
      if (sequence !== renderSequence || !panel.isConnected) return;
      const sources = Array.isArray(data.sources) ? data.sources.filter(path => typeof path === "string" && path.trim()) : [];
      target.innerHTML = sources.length
        ? sources.map((path, index) => `<div class="settings-source"><code class="settings-path">${esc(path)}</code><button type="button" class="subtle" data-settings-source="${index}">${icon("copy")}复制路径</button></div>`).join("")
        : '<p class="settings-note">尚未设置销售数据文件夹，可先在「带货数据」导入表格。</p>';
      $$("[data-settings-source]", target).forEach(button => {
        button.onclick = () => copyText(sources[Number(button.dataset.settingsSource)], "路径已复制");
      });
      const imported = $("#settings-last-import", panel);
      imported.textContent = data.imports?.last ? `最近导入：${readableDate(data.imports.last)}` : "尚未导入销售数据。";
    } catch (error) {
      if (sequence !== renderSequence || !panel.isConnected) return;
      target.replaceChildren();
      const note = document.createElement("p");
      note.className = "settings-note";
      note.textContent = `数据文件夹暂时无法读取：${error.message || "请稍后重试。"}`;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "subtle";
      retry.textContent = "重新读取";
      retry.onclick = () => { retry.disabled = true; void loadSources(panel, sequence); };
      target.append(note, retry);
    }
  }

  function dataState(panel) {
    if (!dataStates.has(panel)) dataStates.set(panel, { status: null, pending: "", hydrated: false, formDirty: false, import: null, importRevision: 0, controllers: new Set() });
    return dataStates.get(panel);
  }

  function preferenceKey(key) {
    return typeof key === "string" && /^(?:case|workbench|native|material)[.-]/.test(key);
  }

  function browserPreferences() {
    const preferences = {};
    try {
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (preferenceKey(key)) {
          const value = localStorage.getItem(key);
          if (typeof value === "string") preferences[key] = value;
        }
      }
    } catch { /* Browser preferences may be unavailable in a private session. */ }
    return preferences;
  }

  async function mirrorPreferences(signal) {
    return api("/api/data/preferences", { method: "POST", body: { preferences: browserPreferences() }, ...(signal ? { signal } : {}) });
  }

  function dataError(panel, message = "") {
    const element = $("#settings-data-error", panel);
    if (!element || !panel.isConnected) return;
    element.textContent = message;
    element.hidden = !message;
  }

  function dataDate(value) {
    if (!value) return "暂无";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
  }

  function dataBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "大小未知";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function updateDataControls(panel) {
    if (!panel.isConnected) return;
    const local = dataState(panel);
    const busy = !!local.pending || !!local.status?.busy;
    const card = $("#settings-data-card", panel);
    card.setAttribute("aria-busy", String(busy));
    $$("button,input", card).forEach(control => { control.disabled = busy; });
    $("#settings-data-backup", panel).disabled = busy || !local.status?.settings?.directory;
    $("#settings-data-open", panel).disabled = busy || !local.status?.settings?.directory;
    $("#settings-data-import-confirm", panel).disabled = busy || !local.import;
    $("#settings-data-import", panel).disabled = busy || !local.import || !$("#settings-data-import-confirm", panel).checked;
    $("#settings-data-refresh", panel).disabled = !!local.statusLoading;
    const progress = $("#settings-data-progress", panel);
    const operation = local.status?.operation || local.status?.operation_kind;
    const action = {export:"导出",backup:"备份",inspect:"检查备份文件",restore:"恢复"}[operation] || "处理";
    progress.textContent = (local.status?.busy && local.status.progress) || local.pending || (local.status?.busy ? `总数据正在${action}，请等待完成。离开页面后可回来检查状态。` : local.notice || "");
    progress.hidden = !progress.textContent;
  }

  function applyDataStatus(panel, result, fillForm = false) {
    if (!panel.isConnected) return;
    if (!result || typeof result.settings !== "object") throw new Error("总数据状态暂时无法读取，请重新检查。");
    const local = dataState(panel);
    if (local.status?.busy && result.busy === false) {
      local.notice = "总数据处理已结束，可以继续操作。若此前没有收到完成回复，请先核对结果。";
      if (local.waitingForData) {dataError(panel);local.waitingForData = false;}
    }
    local.status = result;
    window.WorkbenchMaintenance?.update(result);
    if (fillForm || (!local.hydrated && !local.formDirty)) {
      const settings = result.settings;
      $("#settings-backup-enabled", panel).checked = !!settings.enabled;
      $("#settings-backup-directory", panel).value = settings.directory || "";
      $("#settings-backup-days", panel).value = settings.interval_days ?? 1;
      $("#settings-backup-time", panel).value = settings.time || "02:00";
      $("#settings-backup-keep", panel).value = settings.keep ?? 14;
      local.hydrated = true;
      local.formDirty = false;
    }
    const last = result.last_backup;
    $("#settings-backup-last", panel).textContent = last ? `${dataDate(last.created_at)} · ${dataBytes(last.bytes)}` : "尚未备份";
    $("#settings-backup-last-path", panel).textContent = last?.path || "";
    $("#settings-backup-next", panel).textContent = result.settings.enabled ? dataDate(result.next_backup) : "自动备份未开启";
    const scheduler = result.scheduler || {};
    const capability = !result.settings.enabled
      ? "自动备份未开启，仍可随时手动备份。"
      : scheduler.installed
        ? "已配置系统定时任务，工作台关闭后仍可自动备份；电脑需开机并登录当前 Windows 用户。"
        : "自动备份在工作台服务运行时执行；关闭服务后会在下次打开时补备份。";
    $("#settings-backup-scheduler", panel).textContent = [capability, result.settings.enabled ? scheduler.message : ""].filter(Boolean).join(" ");
    const failure = $("#settings-backup-error", panel);
    failure.textContent = result.last_error ? `最近备份未完成：${result.last_error}` : "";
    failure.hidden = !failure.textContent;
    const backups = Array.isArray(result.backups) ? result.backups : [];
    const list = $("#settings-backup-list", panel);
    list.replaceChildren();
    if (!backups.length) {
      const note = document.createElement("p");
      note.className = "settings-note";
      note.textContent = "备份目录中还没有备份文件。";
      list.append(note);
    } else {
      for (const backup of backups.slice(0, 8)) {
        const item = document.createElement("div");
        item.className = "settings-backup-item";
        const name = document.createElement("strong");
        name.textContent = backup.name || "总数据备份";
        const detail = document.createElement("span");
        detail.textContent = `${dataDate(backup.created_at)} · ${dataBytes(backup.bytes)}`;
        item.append(name, detail);
        list.append(item);
      }
      if (backups.length > 8) {
        const note = document.createElement("p");
        note.className = "settings-note";
        note.textContent = `显示最近 8 份，共 ${backups.length} 份；可打开备份文件夹查看全部。`;
        list.append(note);
      }
    }
    updateDataControls(panel);
  }

  function stopDataPolling() {
    if (!dataPoll) return;
    clearTimeout(dataPoll.timer);
    const local = dataState(dataPoll.panel);
    local.controllers.forEach(controller => controller.abort());
    local.controllers.clear();
    dataPoll = null;
  }

  function scheduleDataPolling(panel) {
    if (!panel.isConnected || !dataPoll || dataPoll.panel !== panel) return;
    clearTimeout(dataPoll.timer);
    dataPoll.timer = setTimeout(() => {
      if (!panel.isConnected || dataPoll?.panel !== panel) { stopDataPolling(); return; }
      void loadDataStatus(panel);
    }, dataState(panel).status?.busy ? 5000 : 30000);
  }

  async function loadDataStatus(panel, { mirror = false } = {}) {
    const sequence = renderSequence;
    const local = dataState(panel);
    local.mirrorPending ||= mirror;
    if (local.statusLoading) return;
    local.statusLoading = true;
    const controller = new AbortController();
    local.controllers.add(controller);
    try {
      const result = await api("/api/data/status", { signal: controller.signal });
      if (!panel.isConnected || sequence !== renderSequence || controller.signal.aborted) return;
      applyDataStatus(panel, result);
      if (local.mirrorPending && !result.busy && state.token) {
        try { await mirrorPreferences(controller.signal); }
        catch (error) {
          if (error.name === "AbortError" || controller.signal.aborted) throw error;
          if (panel.isConnected && sequence === renderSequence) dataError(panel, `浏览器视图设置暂未同步：${error.message || "请稍后重试。"}`);
        }
        local.mirrorPending = false;
      }
    } catch (error) {
      if (panel.isConnected && sequence === renderSequence && error.name !== "AbortError") dataError(panel, error.message || "总数据状态暂时无法读取，请重新检查。");
    } finally {
      local.controllers.delete(controller);
      local.statusLoading = false;
      if (panel.isConnected && sequence === renderSequence) updateDataControls(panel);
      if (panel.isConnected && sequence === renderSequence) scheduleDataPolling(panel);
    }
  }

  async function dataAction(panel, message, action) {
    const local = dataState(panel);
    const sequence = renderSequence;
    if (!panel.isConnected || local.pending || local.status?.busy) return;
    const controller = new AbortController();
    local.controllers.add(controller);
    local.pending = message;
    local.notice = "";
    dataError(panel);
    updateDataControls(panel);
    const active = () => panel.isConnected && sequence === renderSequence && !controller.signal.aborted;
    try { await action(controller.signal, active); }
    catch (error) {
      if (active() && error.name !== "AbortError") {
        const maintenance = window.WorkbenchMaintenance?.isError(error) || error.maintenance === true || (error.status === 409 && /总数据/.test(error.message));
        local.waitingForData = maintenance;
        if (!maintenance) dataError(panel, error.message || "操作未收到完成回复，请先检查总数据状态与结果，再决定是否重试。");
        await loadDataStatus(panel);
      }
    } finally {
      local.controllers.delete(controller);
      local.pending = "";
      if (active()) { updateDataControls(panel); scheduleDataPolling(panel); }
    }
  }

  function resetDataImport(panel) {
    const local = dataState(panel);
    local.import = null;
    ++local.importRevision;
    $("#settings-data-import-confirm", panel).checked = false;
    $("#settings-data-import-preview", panel).hidden = true;
    $("#settings-data-import-summary", panel).textContent = "";
    updateDataControls(panel);
  }

  async function inspectDataImport(panel) {
    resetDataImport(panel);
    const local = dataState(panel);
    const revision = local.importRevision;
    const file = $("#settings-data-file", panel).files?.[0];
    if (!file) return;
    if (!/\.zip$/i.test(file.name)) { dataError(panel, "请选择由总数据导出或备份生成的 ZIP 文件。"); return; }
    await dataAction(panel, "正在检查备份文件，尚未更改现有数据…", async (signal, active) => {
      const result = await api("/api/data/import/inspect", { method: "POST", body: file, headers: { "Content-Type": "application/zip" }, signal });
      if (!active() || revision !== local.importRevision) return;
      if (!result?.import_id || !result.summary) throw new Error("备份文件检查未完成，请重新选择文件。");
      local.import = result;
      const summary = result.summary;
      const components = Array.isArray(summary.components) ? summary.components.filter(value => typeof value === "string").join("、") : "全部工作台数据";
      $("#settings-data-import-summary", panel).textContent = `${file.name}\n备份时间：${dataDate(summary.created_at)}\n${summary.files ?? 0} 个文件 · ${dataBytes(summary.bytes)}\n内容：${components}`;
      $("#settings-data-import-preview", panel).hidden = false;
    });
  }

  function showImportedData(panel, result) {
    let preferenceError = false;
    if (result.preferences && typeof result.preferences === "object" && !Array.isArray(result.preferences)) {
      try {
        // An imported preference set replaces this workbench's browser preferences.
        const keys = [];
        for (let index = 0; index < localStorage.length; index++) {
          const key = localStorage.key(index);
          if (preferenceKey(key)) keys.push(key);
        }
        keys.forEach(key => localStorage.removeItem(key));
        Object.entries(result.preferences).forEach(([key, value]) => {
          if (preferenceKey(key) && typeof value === "string") localStorage.setItem(key, value);
        });
      } catch { preferenceError = true; }
    }
    const dialog = document.createElement("dialog");
    dialog.className = "settings-data-result";
    dialog.setAttribute("aria-labelledby", "settings-data-result-title");
    const title = document.createElement("h2");
    title.id = "settings-data-result-title";
    title.textContent = "总数据已恢复";
    const message = document.createElement("p");
    message.textContent = [result.message || "全部产品的数据已恢复，刷新后查看。", result.recovery_path ? `导入前的数据已备份至：${result.recovery_path}` : "", preferenceError ? "浏览器视图设置未能完整恢复；总数据已恢复。" : ""].filter(Boolean).join("\n");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "primary";
    button.textContent = "刷新并查看数据";
    button.onclick = () => dialog.close();
    dialog.addEventListener("close", () => location.reload(), { once: true });
    dialog.append(title, message, button);
    panel.append(dialog);
    dialog.showModal();
  }

  function bindDataSettings(panel) {
    dataPoll = { panel, timer: null };
    $("#settings-backup-form", panel).oninput = () => { dataState(panel).formDirty = true; };
    $("#settings-data-refresh", panel).onclick = () => { dataError(panel); void loadDataStatus(panel); };
    $("#settings-data-export", panel).onclick = () => dataAction(panel, "正在打包全部产品和媒体，文件较多时请稍候…", async (signal, active) => {
      const result = await api("/api/data/export", { method: "POST", body: { preferences: browserPreferences() }, signal });
      if (!active()) return;
      if (typeof result?.download_url !== "string" || !/^\/api\/data\/download\/[A-Za-z0-9_-]+$/.test(result.download_url)) throw new Error("导出文件下载地址无效，请重新导出。");
      const link = document.createElement("a");
      link.href = result.download_url;
      link.download = result.archive?.name || "workbench-data.zip";
      panel.append(link);
      link.click();
      link.remove();
      toast("总数据已打包，正在下载 ZIP 文件。");
    });
    $("#settings-data-pick", panel).onclick = () => dataAction(panel, "正在选择本地备份文件夹…", async (signal, active) => {
      const result = await api("/api/data/pick-directory", { method: "POST", body: {}, signal });
      if (active() && result?.path && !result.cancelled) {
        $("#settings-backup-directory", panel).value = result.path;
        dataState(panel).formDirty = true;
      }
    });
    $("#settings-data-open", panel).onclick = () => dataAction(panel, "正在打开备份文件夹…", signal => api("/api/data/open-directory", { method: "POST", body: {}, signal }));
    $("#settings-backup-form", panel).onsubmit = event => {
      event.preventDefault();
      const enabled = $("#settings-backup-enabled", panel).checked;
      const directory = $("#settings-backup-directory", panel).value.trim();
      const interval = Number($("#settings-backup-days", panel).value);
      const time = $("#settings-backup-time", panel).value;
      const keep = Number($("#settings-backup-keep", panel).value);
      if (enabled && !directory) { dataError(panel, "开启自动备份前，请选择本地备份文件夹。"); return; }
      if (directory && !/^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(directory)) { dataError(panel, "请输入完整的本地文件夹路径，或点击「选择文件夹」。"); return; }
      if (!Number.isInteger(interval) || interval < 1 || interval > 365 || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time) || !Number.isInteger(keep) || keep < 0 || keep > 3650) { dataError(panel, "请填写 1–365 天的间隔、有效备份时间及 0–3650 的保留份数。"); return; }
      void dataAction(panel, "正在保存备份设置…", async (signal, active) => {
        await mirrorPreferences(signal);
        if (!active()) return;
        const result = await api("/api/data/settings", { method: "POST", body: { enabled, directory, interval_days: interval, time, keep }, signal });
        if (active()) { applyDataStatus(panel, result, true); toast("备份设置已保存。"); }
      });
    };
    $("#settings-data-backup", panel).onclick = () => dataAction(panel, "正在将全部产品和媒体备份到本地，请稍候…", async (signal, active) => {
      await mirrorPreferences(signal);
      if (!active()) return;
      const result = await api("/api/data/backup", { method: "POST", body: {}, signal });
      if (active()) { applyDataStatus(panel, result?.status); toast("总数据备份已保存到本地。"); }
    });
    $("#settings-data-file", panel).onchange = () => { void inspectDataImport(panel); };
    $("#settings-data-import-confirm", panel).onchange = () => updateDataControls(panel);
    $("#settings-data-import", panel).onclick = () => {
      const checked = dataState(panel).import;
      if (!checked || !$("#settings-data-import-confirm", panel).checked) return;
      void dataAction(panel, "正在恢复总数据；请等待完成…", async (signal, active) => {
        await mirrorPreferences(signal);
        if (!active()) return;
        const result = await api("/api/data/import", { method: "POST", body: { import_id: checked.import_id, confirm: true }, signal });
        if (active()) { resetDataImport(panel); showImportedData(panel, result || {}); }
      });
    };
    updateDataControls(panel);
    void loadDataStatus(panel, { mirror: true });
  }

  function renderDataSettings() {
    return `<section class="section-card settings-card" id="settings-data-card" aria-labelledby="settings-data-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("folder")}</span><h2 id="settings-data-heading">总数据导入、导出与备份</h2></div><p class="settings-note">包含全部产品的案例、卖点与方向、脚本与素材、销售数据、视频附件，以及工作台视图设置。导出和本地备份均生成 ZIP 文件，可在这里完整恢复。</p>
      <div class="settings-data-transfer"><div><h3>导出总数据</h3><p class="settings-note">打包当前工作台的完整数据，下载到浏览器的保存位置。</p><button type="button" class="secondary compact" id="settings-data-export">导出全部数据</button></div><div><h3>从文件恢复</h3><label class="settings-data-file-label" for="settings-data-file">选择总数据 ZIP 文件</label><input id="settings-data-file" type="file" accept=".zip,application/zip"><div id="settings-data-import-preview" class="settings-data-import-preview" hidden><p id="settings-data-import-summary"></p><p class="settings-data-warning">恢复会整体替换当前工作台全部产品的数据。导入前会自动保存一份恢复备份；请核对文件内容后再继续。</p><label class="settings-data-check"><input type="checkbox" id="settings-data-import-confirm">我已核对文件，同意替换当前全部数据</label><button type="button" class="primary compact" id="settings-data-import" disabled>确认恢复总数据</button></div></div></div>
      <form id="settings-backup-form" class="settings-backup-form"><div class="settings-backup-heading"><h3>本地自动备份</h3><label class="settings-data-check"><input type="checkbox" id="settings-backup-enabled">开启自动备份</label></div><label class="settings-backup-directory-label" for="settings-backup-directory">备份文件夹</label><div class="settings-backup-directory"><input type="text" id="settings-backup-directory" placeholder="例如 D:\\TK工作台备份" autocomplete="off"><button type="button" class="secondary compact" id="settings-data-pick">选择文件夹</button></div><p class="settings-note">可选择文件夹或填写完整路径。保存设置后，立即备份会使用该目录。</p><div class="settings-backup-grid"><label for="settings-backup-days">备份间隔（天）<input type="number" id="settings-backup-days" min="1" max="365" step="1" value="1" required></label><label for="settings-backup-time">备份时间（本机时间）<input type="time" id="settings-backup-time" value="02:00" required></label><label for="settings-backup-keep">保留最近多少份<input type="number" id="settings-backup-keep" min="0" max="3650" step="1" value="14" required><small>填 0 保留全部备份</small></label></div><div class="settings-actions"><button type="submit" class="primary compact">保存备份设置</button><button type="button" class="secondary compact" id="settings-data-backup" disabled>立即备份</button><button type="button" class="subtle" id="settings-data-open" disabled>打开备份文件夹</button><button type="button" class="subtle" id="settings-data-refresh">检查备份状态</button></div></form>
      <p id="settings-data-progress" class="settings-data-progress" role="status" aria-live="polite" hidden></p><p id="settings-data-error" class="settings-data-warning" role="alert" hidden></p><div class="settings-backup-state"><dl><div><dt>最近备份</dt><dd id="settings-backup-last">正在读取…</dd></div><div><dt>下次自动备份</dt><dd id="settings-backup-next">正在读取…</dd></div></dl><code class="settings-path" id="settings-backup-last-path"></code><p class="settings-note" id="settings-backup-scheduler"></p><p class="settings-data-warning" id="settings-backup-error" role="alert" hidden></p><details class="settings-backup-history"><summary>最近的本地备份</summary><div id="settings-backup-list"></div></details></div></section>`;
  }

  // The page is long, so a section bar jumps to each group; buttons scroll instead of changing the routed hash.
  const settingsSections = [
    ["settings-group-ai", "模型与连接", () => true],
    ["settings-group-remote", "公网账号", () => true],
    ["settings-group-alerts", "任务提醒", () => Boolean(window.TaskActivity?.settingsMarkup?.())],
    ["settings-group-data", "数据与备份", () => true],
    ["settings-group-files", "本地文件", () => true],
    ["settings-group-views", "保存的视图", () => true],
  ];

  function render() {
    stopLoginPolling();
    stopDataPolling();
    const sequence = ++renderSequence;
    $("#breadcrumb").innerHTML = "设置";
    $("#content").innerHTML = `<div class="settings-page"><div class="page-heading"><div><span class="eyebrow">WORKSPACE SETTINGS</span><h1>设置</h1><p>设置全局默认模型，管理协作连接、保存的视图与本地文件。</p></div></div>
      <nav class="settings-sections" aria-label="设置分区">${settingsSections.filter(([, , shown]) => shown()).map(([id, label]) => `<button type="button" data-settings-jump="${id}">${label}</button>`).join("")}</nav>
      <div class="settings-group" id="settings-group-ai">${renderModelSettings()}<div class="settings-overview">
          <section class="section-card settings-card" aria-labelledby="settings-ai-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("bot")}</span><h2 id="settings-ai-heading">AI 协作</h2></div><p id="settings-codex-status" class="settings-status" data-status="loading" role="status">正在检查…</p><p id="settings-codex-detail" class="settings-note"></p><p class="settings-note">写稿、审稿和复盘沿用全局默认模型。使用本机 Codex 的登录与额度。</p><div class="settings-actions"><button type="button" class="secondary compact" id="settings-check-codex">${icon("refresh")}重新检查</button><a class="subtle settings-link" href="#ai">查看协作记录 ${icon("arrow")}</a></div></section>
          <section class="section-card settings-card" id="settings-claude-card" aria-labelledby="settings-claude-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("spark")}</span><h2 id="settings-claude-heading">Claude 写稿</h2></div><p id="settings-claude-status" class="settings-status" data-status="loading" role="status">正在检查…</p><p id="settings-claude-detail" class="settings-note"></p><p class="settings-note">使用 Claude 桌面版自带的调用程序与同一账号的客户端额度，无需填写 key。首次使用点「连接 Claude 订阅」完成一次浏览器授权；桌面聊天登录与调用授权分别保存。在上方「全局默认模型」中选择 Claude；选用客户端默认时，默认沿用本机调用设置。</p><div class="settings-actions"><button type="button" class="primary compact" id="settings-claude-login" disabled>连接 Claude 订阅</button><button type="button" class="secondary compact" id="settings-claude-check">${icon("refresh")}检查 Claude</button></div><div id="settings-claude-recovery" class="settings-claude-recovery" hidden><p class="settings-note" id="settings-claude-url-note"></p><div class="settings-actions"><button type="button" class="secondary compact" id="settings-claude-copy" disabled>${icon("copy")}复制授权链接</button><a class="secondary compact settings-link" id="settings-claude-reopen" target="_blank" rel="noopener noreferrer" hidden>重新打开授权页 ${icon("arrow")}</a></div><form id="settings-claude-callback-form" class="settings-claude-callback-form"><label for="settings-claude-callback">填写回调链接或授权码</label><p class="settings-note" id="settings-claude-callback-help">完整回调链接，或完整授权码（包含 # 后的内容）</p><textarea id="settings-claude-callback" rows="3" autocomplete="off" spellcheck="false" aria-describedby="settings-claude-callback-help" placeholder="完整回调链接或授权码" disabled></textarea><div class="settings-actions"><button type="submit" class="primary compact" id="settings-claude-submit" disabled>提交回调</button><button type="button" class="subtle" id="settings-claude-cancel" disabled>取消本次授权</button></div></form></div><p id="settings-claude-login-error" class="settings-note settings-claude-error" role="alert" hidden></p></section>
      </div></div>
      <div class="settings-group" id="settings-group-remote">${renderRemoteAccount()}</div>
      <div class="settings-group" id="settings-group-alerts">${window.TaskActivity?.settingsMarkup?.() || ""}</div>
      <div class="settings-group" id="settings-group-data">${renderDataSettings()}</div>
      <div class="settings-group" id="settings-group-files">
        <section class="section-card settings-card" aria-labelledby="settings-files-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("folder")}</span><h2 id="settings-files-heading">本地文件</h2></div><p class="settings-note"><a href="#settings-group-data" data-settings-backup-link>总数据备份</a>包含以下业务资料；仅保留阅读目录不能完整恢复。</p><div class="settings-locations">${locations.map(renderLocation).join("")}</div><p class="settings-note settings-claude-error" data-location-error role="alert" hidden><span></span> <button type="button" class="subtle">重新读取路径</button></p></section>
        <section class="section-card settings-card" aria-labelledby="settings-sources-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("chart")}</span><h2 id="settings-sources-heading">销售数据文件夹</h2></div><p class="settings-note">将后台导出的表格放进以下文件夹，再到「视频数据」同步数据。</p><div id="settings-data-sources" aria-live="polite"><p class="settings-note">正在读取文件夹…</p></div><div class="settings-source-footer"><p class="settings-note" id="settings-last-import"></p><a class="subtle settings-link" href="#videos">前往视频数据 ${icon("arrow")}</a></div></section>
        <section class="section-card settings-card" aria-labelledby="settings-extension-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("link")}</span><h2 id="settings-extension-heading">TK 看板插件</h2></div><p class="settings-note">在插件设置「案例工作台」里填入下面的地址和连接码，之后在 TikTok 页面点「推送到工作台」，工作台里所有累计播放都改用公开页数字。只收工作台已有的视频；首两周播放和点击率等仍按销售数据表计算；参考价值以人工复盘分类为准。</p><div id="settings-extension-link" aria-live="polite"><p class="settings-note">正在读取连接码…</p></div></section>
      </div>
      <div class="settings-group" id="settings-group-views">
          <section class="section-card settings-card" aria-labelledby="settings-nav-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("layers")}</span><h2 id="settings-nav-heading">保存的视图</h2></div><p class="settings-note">把常用的案例库筛选保存为视图，管理名称、顺序与显示方式。</p><p class="settings-note">视图保存在当前浏览器，原栏目与收纳案例继续保留。</p><div class="settings-actions"><button type="button" class="secondary compact" id="settings-navigation">${icon("edit")}管理保存的视图</button></div></section>
      </div>
    </div>`;
    const panel = $(".settings-page");
    $$("[data-settings-jump]", panel).forEach(button => {
      button.onclick = () => document.getElementById(button.dataset.settingsJump)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    $$("[data-settings-copy]", panel).forEach(button => {
      button.onclick = () => copyText(state.paths[button.dataset.settingsCopy], "路径已复制");
    });
    $$("[data-settings-open]", panel).forEach(button => {
      button.onclick = () => openFolder({ target: button.dataset.settingsOpen });
    });
    $("#settings-navigation", panel).onclick = () => {
      if (typeof window.LibraryViews?.openManager === "function") window.LibraryViews.openManager();
      else if (typeof window.openNavigationSettings === "function") window.openNavigationSettings();
      else toast("视图设置暂时无法打开，请刷新页面后重试。", true);
    };
    $("#settings-check-codex", panel).onclick = () => checkCodex(panel);
    void checkCodex(panel);
    $("#settings-claude-check", panel).onclick = () => checkCodex(panel);
    $("#settings-claude-login", panel).onclick = () => connectClaude(panel);
    $("#settings-claude-copy", panel).onclick = () => {
      const url = officialAuthorizationUrl(panelState(panel).login?.url);
      if (url) void copyText(url, "授权链接已复制");
    };
    $("#settings-claude-callback", panel).oninput = () => renderLoginControls(panel);
    $("#settings-claude-callback-form", panel).onsubmit = event => {
      event.preventDefault();
      void submitClaudeLoginAction(panel, "callback");
    };
    $("#settings-claude-cancel", panel).onclick = () => submitClaudeLoginAction(panel, "cancel");
    panel.querySelector("[data-settings-backup-link]").onclick = event => { event.preventDefault(); panel.querySelector("#settings-group-data")?.scrollIntoView({block:"start"}); };
    void loadLocations(panel, sequence);
    void loadSources(panel, sequence);
    void loadExtensionLink(panel, sequence);
    bindDataSettings(panel);
    bindModelSettings(panel);
    bindRemoteAccount(panel);
    window.TaskActivity?.mountSettings?.(panel);
  }

  window.WorkbenchSettings = { render, dataStatusActive:() => !!dataPoll?.panel?.isConnected,
    dataActionActive:() => !!dataPoll?.panel?.isConnected && (!!dataState(dataPoll.panel).pending || !!$(".settings-data-result[open]",dataPoll.panel)) };
  window.addEventListener("hashchange", () => { ++renderSequence; stopLoginPolling(); stopDataPolling(); });
})();
