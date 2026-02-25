// app/static/auth.js
(function () {
  "use strict";

  const TOKEN_KEY = "ogx_token";

  function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
  function setToken(t) { try { localStorage.setItem(TOKEN_KEY, t); } catch {} }
  function clearToken() { try { localStorage.removeItem(TOKEN_KEY); } catch {} }

  // Attach token to every fetch (monkey-patch)
  const _origFetch = window.fetch;
  window.fetch = function (url, opts) {
    opts = opts || {};
    const tok = getToken();
    if (tok) {
      opts.headers = Object.assign({ Authorization: "Bearer " + tok }, opts.headers || {});
    }
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

  async function doLogout() {
    clearToken();
    window.location.reload();
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

  // Modal open/close
  const modal = document.getElementById("auth-modal");
  const backdrop = document.getElementById("auth-backdrop");
  const btnOpen = document.getElementById("btn-login-open");
  const btnClose = document.getElementById("btn-login-close");
  const btnLogout = document.getElementById("btn-logout");

  if (btnOpen) btnOpen.addEventListener("click", () => { if (modal) modal.style.display = "block"; });
  if (btnClose) btnClose.addEventListener("click", () => { if (modal) modal.style.display = "none"; });
  if (backdrop) backdrop.addEventListener("click", () => { if (modal) modal.style.display = "none"; });
  if (btnLogout) btnLogout.addEventListener("click", doLogout);

  // Login submit
  async function handleLogin() {
    const u = (document.getElementById("inp-user") || {}).value || "";
    const p = (document.getElementById("inp-pass") || {}).value || "";
    if (!u || !p) { showError("Username and password required."); return; }
    hideError();
    const data = await doLogin(u.trim().toLowerCase(), p);
    if (data.ok) {
      setToken(data.token);
      window.location.reload();
    } else {
      showError(data.error || "Login failed.");
    }
  }

  const btnDoLogin = document.getElementById("btn-do-login");
  if (btnDoLogin) btnDoLogin.addEventListener("click", handleLogin);

  // Enter key
  ["inp-user", "inp-pass"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") handleLogin(); });
  });

  // On load: send token in cookie for server-side template rendering
  const tok = getToken();
  if (tok) {
    document.cookie = `ogx_token=${tok}; path=/; SameSite=Strict; Max-Age=86400`;
  }
})();
