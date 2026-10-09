"use strict";

// Preferences and file locations share the existing workbench APIs.
(() => {
  let renderSequence = 0;
  const panelStates = new WeakMap();
  let loginPoll = null;
  const loginPollInterval = 2500;
  const loginPollTimeout = 5 * 60 * 1000;
  const locations = [
    ["records", "正式案例记录", "原始资料、当前版本与每次修订。"],
    ["directions", "卖点与方向", "卖点表、方向定义、视频关联与迁移记录；请一同备份。"],
    ["creation", "创作与素材", "采纳的脚本版本、素材篮、评论主题和待写计划；请一同备份。"],
    ["evidence", "视频与证据附件", "已导入的视频、截图和转写文件。"],
    ["library", "中文阅读目录", "随案例保存同步更新的阅读文件。"],
  ];

  function renderLocation([key, label, note]) {
    const path = typeof state.paths?.[key] === "string" ? state.paths[key] : "";
    return `<div class="settings-location"><div class="settings-location-copy"><h3>${label}</h3><p>${note}</p><code class="settings-path">${esc(path || "路径暂时无法读取")}</code></div><div class="settings-actions"><button type="button" class="subtle" data-settings-copy="${key}" ${path ? "" : "disabled"}>${icon("copy")}复制路径</button><button type="button" class="secondary compact" data-settings-open="${key}" ${path ? "" : "disabled"}>${icon("folder")}打开文件夹</button></div></div>`;
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

  async function loadSources(panel, sequence) {
    const target = $("#settings-data-sources", panel);
    try {
      const data = await api("/api/performance/summary");
      if (sequence !== renderSequence || !panel.isConnected) return;
      const sources = Array.isArray(data.sources) ? data.sources.filter(path => typeof path === "string" && path.trim()) : [];
      target.innerHTML = sources.length
        ? sources.map((path, index) => `<div class="settings-source"><code class="settings-path">${esc(path)}</code><button type="button" class="subtle" data-settings-source="${index}">${icon("copy")}复制路径</button></div>`).join("")
        : '<p class="settings-note">尚未设置销售数据文件夹，可先在「视频数据」导入表格。</p>';
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

  function render() {
    stopLoginPolling();
    const sequence = ++renderSequence;
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 设置";
    $("#content").innerHTML = `<div class="settings-page"><div class="page-heading"><div><span class="eyebrow">WORKSPACE SETTINGS</span><h1>设置</h1><p>查看协作状态，管理保存的视图与本地文件。</p></div></div>
      <div class="settings-overview">
        <section class="section-card settings-card" aria-labelledby="settings-ai-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("bot")}</span><h2 id="settings-ai-heading">AI 协作</h2></div><p id="settings-codex-status" class="settings-status" data-status="loading" role="status">正在检查…</p><p id="settings-codex-detail" class="settings-note"></p><p class="settings-note">发起协作时选择模型、思考强度与技能。使用本机 Codex 的登录与设置。</p><div class="settings-actions"><button type="button" class="secondary compact" id="settings-check-codex">${icon("refresh")}重新检查</button><a class="subtle settings-link" href="#ai">查看协作记录 ${icon("arrow")}</a></div></section>
        <section class="section-card settings-card" id="settings-claude-card" aria-labelledby="settings-claude-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("spark")}</span><h2 id="settings-claude-heading">Claude 写稿</h2></div><p id="settings-claude-status" class="settings-status" data-status="loading" role="status">正在检查…</p><p id="settings-claude-detail" class="settings-note"></p><p class="settings-note">使用 Claude 桌面版自带的调用程序与同一账号的客户端额度，无需填写 key。首次使用点「连接 Claude 订阅」完成一次浏览器授权；桌面聊天登录与调用授权分别保存。在写稿的「模型」里选择 Claude；默认沿用本机调用设置。</p><div class="settings-actions"><button type="button" class="primary compact" id="settings-claude-login" disabled>连接 Claude 订阅</button><button type="button" class="secondary compact" id="settings-claude-check">${icon("refresh")}检查 Claude</button></div><div id="settings-claude-recovery" class="settings-claude-recovery" hidden><p class="settings-note" id="settings-claude-url-note"></p><div class="settings-actions"><button type="button" class="secondary compact" id="settings-claude-copy" disabled>${icon("copy")}复制授权链接</button><a class="secondary compact settings-link" id="settings-claude-reopen" target="_blank" rel="noopener noreferrer" hidden>重新打开授权页 ${icon("arrow")}</a></div><form id="settings-claude-callback-form" class="settings-claude-callback-form"><label for="settings-claude-callback">填写回调链接或授权码</label><p class="settings-note" id="settings-claude-callback-help">完整回调链接，或完整授权码（包含 # 后的内容）</p><textarea id="settings-claude-callback" rows="3" autocomplete="off" spellcheck="false" aria-describedby="settings-claude-callback-help" placeholder="完整回调链接或授权码" disabled></textarea><div class="settings-actions"><button type="submit" class="primary compact" id="settings-claude-submit" disabled>提交回调</button><button type="button" class="subtle" id="settings-claude-cancel" disabled>取消本次授权</button></div></form></div><p id="settings-claude-login-error" class="settings-note settings-claude-error" role="alert" hidden></p></section>
        <section class="section-card settings-card" aria-labelledby="settings-nav-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("layers")}</span><h2 id="settings-nav-heading">保存的视图</h2></div><p class="settings-note">把常用的案例库筛选保存为视图，管理名称、顺序与显示方式。</p><p class="settings-note">视图保存在当前浏览器，原栏目与收纳案例继续保留。</p><div class="settings-actions"><button type="button" class="secondary compact" id="settings-navigation">${icon("edit")}管理保存的视图</button></div></section>
      </div>
      <section class="section-card settings-card" aria-labelledby="settings-files-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("folder")}</span><h2 id="settings-files-heading">本地文件</h2></div><p class="settings-note">备份时请同时保留案例记录、卖点与方向、创作与素材、视频附件，以及评论手动记录。</p><div class="settings-locations">${locations.map(renderLocation).join("")}</div></section>
      <section class="section-card settings-card" aria-labelledby="settings-sources-heading"><div class="settings-card-heading"><span class="settings-symbol">${icon("chart")}</span><h2 id="settings-sources-heading">销售数据文件夹</h2></div><p class="settings-note">将后台导出的表格放进以下文件夹，再到「视频数据」同步数据。</p><div id="settings-data-sources" aria-live="polite"><p class="settings-note">正在读取文件夹…</p></div><div class="settings-source-footer"><p class="settings-note" id="settings-last-import"></p><a class="subtle settings-link" href="#videos">前往视频数据 ${icon("arrow")}</a></div></section>
    </div>`;
    const panel = $(".settings-page");
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
    void loadSources(panel, sequence);
  }

  window.WorkbenchSettings = { render };
  window.addEventListener("hashchange", () => { ++renderSequence; stopLoginPolling(); });
})();
