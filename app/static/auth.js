// app/static/auth.js
(function () {
  "use strict";

  const TOKEN_KEY = "ogx_token";
  const COOKIE_NAME = "ogx_token";

  function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
  function setToken(t) {
    try { localStorage.setItem(TOKEN_KEY, t); } catch {}
    document.cookie = `${COOKIE_NAME}=${t}; path=/; SameSite=Strict; Max-Age=86400`;
  }
  function clearToken() {
    try { localStorage.removeItem(TOKEN_KEY); } catch {}
    document.cookie = `${COOKIE_NAME}=; path=/; SameSite=Strict; Max-Age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
  }

  const _origFetch = window.fetch;
  window.fetch = function (url, opts) {
    opts = opts || {};
    const tok = getToken();
    if (tok) opts.headers = Object.assign({ Authorization: "Bearer " + tok }, opts.headers || {});
    return _origFetch.call(this, url, opts);
  };

  async function doLogin(username, password) {
    const res = await _origFetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    return res.json();
  }

  function doLogout() {
    clearToken();
    window.location.href = "/";
  }

  function showError(msg) {
    const el = document.getElementById("auth-error");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
  }

  function hideError() {
    const el = document.getElementById("auth-error");
    if (el) el.style.display = "none";
  }

  const modal = document.getElementById("auth-modal");
  const backdrop = document.getElementById("auth-backdrop");
  const btnOpen = document.getElementById("btn-login-open");
  const btnClose = document.getElementById("btn-login-close");
  const btnLogout = document.getElementById("btn-logout");

  if (btnOpen) btnOpen.addEventListener("click", () => { if (modal) modal.style.display = "block"; });
  if (btnClose) btnClose.addEventListener("click", () => { if (modal) modal.style.display = "none"; });
  if (backdrop) backdrop.addEventListener("click", () => { if (modal) modal.style.display = "none"; });
  if (btnLogout) btnLogout.addEventListener("click", doLogout);

  async function handleLogin() {
    const u = (document.getElementById("inp-user") || {}).value || "";
    const p = (document.getElementById("inp-pass") || {}).value || "";
    if (!u || !p) { showError("Username and password required."); return; }
    hideError();
    const data = await doLogin(u.trim().toLowerCase(), p);
    if (data.ok) {
      setToken(data.token);
      window.location.href = "/";
    } else {
      showError(data.error || "Login failed.");
    }
  }

  const btnDoLogin = document.getElementById("btn-do-login");
  if (btnDoLogin) btnDoLogin.addEventListener("click", handleLogin);

  ["inp-user", "inp-pass"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") handleLogin(); });
  });

  const tok = getToken();
  if (tok) document.cookie = `${COOKIE_NAME}=${tok}; path=/; SameSite=Strict; Max-Age=86400`;
})();
