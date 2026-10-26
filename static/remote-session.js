"use strict";

(() => {
  const host = location.hostname.toLowerCase().replace(/\.$/, "");
  if (window.PublicTextMode || host === "localhost" || host.endsWith(".localhost") ||
      host === "[::1]" || host === "::1" || /^127(?:\.\d{1,3}){3}$/.test(host)) return;

  const originalFetch = window.fetch.bind(window);
  let redirecting = false;

  function login() {
    if (redirecting) return;
    redirecting = true;
    const next = location.pathname + location.search + location.hash;
    location.replace("/auth/login?next=" + encodeURIComponent(next));
  }

  // Install before DOMContentLoaded starts the application's initial requests.
  // Return the original response; mutations are never replayed after expiry.
  window.fetch = async function (...args) {
    const response = await originalFetch(...args);
    if (response.status === 401 && response.headers.get("X-Workbench-Auth") === "required") {
      try {
        const input = args[0];
        const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, location.href);
        if (url.origin === location.origin && !url.pathname.startsWith("/auth/")) login();
      } catch { /* Leave unrelated or non-URL requests untouched. */ }
    }
    return response;
  };

  function showAccount(username) {
    const target = document.querySelector(".topbar-right");
    if (!target || document.getElementById("remote-account")) return;
    const account = document.createElement("details");
    account.id = "remote-account";
    account.className = "remote-account";
    const summary = document.createElement("summary");
    summary.setAttribute("aria-label", "账号：" + username);
    summary.title = username;
    const avatar = document.createElement("span");
    avatar.className = "remote-account-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = Array.from(username)[0]?.toUpperCase() || "我";
    const short = document.createElement("span");
    short.className = "remote-account-short";
    short.textContent = username;
    summary.append(avatar, short);
    const menu = document.createElement("div");
    menu.className = "remote-account-menu";
    const label = document.createElement("span");
    label.textContent = "当前账号";
    const name = document.createElement("p");
    name.className = "remote-account-name";
    name.textContent = username;
    const logout = document.createElement("button");
    logout.type = "button";
    logout.className = "secondary compact remote-account-logout";
    logout.textContent = "退出登录";
    const error = document.createElement("p");
    error.className = "remote-account-error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    logout.addEventListener("click", async () => {
      if (logout.disabled) return;
      logout.disabled = true;
      logout.textContent = "正在退出…";
      error.hidden = true;
      try {
        const response = await originalFetch("/auth/logout", {
          method: "POST", credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: "{}",
        });
        if (response.ok || response.status === 401) {
          login();
          return;
        }
        throw new Error("logout unavailable");
      } catch {
        error.textContent = "暂时无法退出，请重试。";
        error.hidden = false;
        logout.disabled = false;
        logout.textContent = "退出登录";
      }
    });
    menu.append(label, name, logout, error);
    account.append(summary, menu);
    target.append(account);
    document.addEventListener("click", (event) => {
      if (account.open && !account.contains(event.target)) account.open = false;
    });
    account.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { account.open = false; summary.focus(); }
    });
  }

  void originalFetch("/auth/session", {
    credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" },
  }).then(async (response) => {
    if (response.status === 401) {
      const data = await response.json().catch(() => null);
      if (data?.login_url === "/auth/login" || response.headers.get("X-Workbench-Auth") === "required") login();
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (data.authenticated === true && typeof data.username === "string") showAccount(data.username);
  }).catch(() => { /* Cloudflare-only and temporarily unavailable sessions stay quiet. */ });
})();
