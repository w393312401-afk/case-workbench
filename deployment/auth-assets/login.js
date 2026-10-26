"use strict";

(() => {
  const form = document.getElementById("login-form");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const showPassword = document.getElementById("show-password");
  const submit = document.getElementById("login-submit");
  const submitLabel = document.getElementById("submit-label");
  const spinner = document.querySelector(".spinner");
  const error = document.getElementById("login-error");
  const status = document.getElementById("login-status");
  const availability = document.getElementById("login-availability");
  const guestBrowse = document.getElementById("guest-browse");
  let submitting = false;
  let checkingMode = true;
  let loginAvailable = true;

  function destination(value) {
    if (!value || !value.startsWith("/") || value.startsWith("//")) return "/";
    // Reject encoded backslashes and control characters as well as literal ones.
    let decoded = value;
    try {
      for (let round = 0; round < 3; round++) {
        if (/[\\\u0000-\u0020\u007f]/.test(decoded) || decoded.startsWith("//")) return "/";
        const next = decodeURIComponent(decoded);
        if (next === decoded) break;
        decoded = next;
      }
      if (/[\\\u0000-\u0020\u007f]/.test(decoded) || decoded.startsWith("//")) return "/";
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || /^\/auth(?:\/|$)/.test(url.pathname)) return "/";
      return url.pathname + url.search + url.hash;
    } catch {
      return "/";
    }
  }

  const requestedNext = new URLSearchParams(location.search).get("next");
  // HTTP redirects cannot see the original fragment; browsers inherit it onto
  // the login URL when the redirect has none. Carry it back after authentication.
  const next = destination((requestedNext || "/") +
    (requestedNext?.includes("#") ? "" : location.hash));
  showPassword.addEventListener("click", () => {
    const visible = password.type === "password";
    password.type = visible ? "text" : "password";
    showPassword.textContent = visible ? "隐藏" : "显示";
    showPassword.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
    showPassword.setAttribute("aria-pressed", String(visible));
  });

  function busy(value) {
    submitting = value;
    form.setAttribute("aria-busy", String(value));
    const disabled = value || checkingMode || !loginAvailable;
    username.disabled = disabled;
    password.disabled = disabled;
    submit.disabled = disabled;
    showPassword.disabled = disabled;
    spinner.hidden = !value;
    submitLabel.textContent = value ? "正在登录…" : "登录工作台";
    status.textContent = value ? "正在验证账号和密码" : "";
  }

  function failure(message) {
    error.textContent = message;
    error.hidden = false;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || checkingMode || !loginAvailable || !form.reportValidity()) return;
    const body = JSON.stringify({ username: username.value.trim(), password: password.value });
    error.hidden = true;
    error.textContent = "";
    busy(true);
    try {
      const response = await fetch("/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        cache: "no-store",
        body,
      });
      if (response.ok) {
        const result = await response.json();
        if (result.ok === true) {
          password.value = "";
          status.textContent = "登录成功，正在打开工作台";
          location.replace(next);
          return;
        }
      }
      if (response.status === 401) {
        failure("账号或密码错误，请重新输入。");
        password.value = "";
      } else if (response.status === 429) {
        const retry = Number(response.headers.get("Retry-After"));
        failure(Number.isFinite(retry) && retry > 0
          ? `尝试次数较多，请 ${Math.ceil(retry)} 秒后重试。`
          : "尝试次数较多，请稍后再试。");
      } else {
        failure("登录服务暂时不可用，请稍后重试。");
      }
    } catch {
      failure("暂时无法连接工作台，请检查网络后重试。");
    } finally {
      busy(false);
    }
  });

  busy(false);
  const modeController = new AbortController();
  const modeTimeout = setTimeout(() => modeController.abort(), 5000);
  void fetch("/auth/mode", { credentials: "same-origin", cache: "no-store",
    headers: { "Accept": "application/json" }, signal: modeController.signal,
  }).then(async response => {
    if (!response.ok) return;
    const mode = await response.json();
    guestBrowse.hidden = mode.guest_enabled !== true;
    if (mode.configured === false) {
      loginAvailable = false;
      username.value = "";
      password.value = "";
      availability.textContent = "尚未设置账号，请在本机工作台设置后登录。";
      availability.hidden = false;
    }
  }).catch(() => { /* Older proxies do not expose this optional capability. */ }).finally(() => {
    clearTimeout(modeTimeout);
    checkingMode = false;
    busy(false);
  });
})();
