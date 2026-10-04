/* Shared helpers. Every dynamic value that reaches innerHTML MUST go through App.esc(). */
(function () {
  "use strict";
  const KEY_USER = "migration.user", KEY_API = "migration.apikey";
  const App = (window.App = {});

  App.$ = (sel, root) => (root || document).querySelector(sel);
  App.esc = (v) => String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  App.pct = (x, d) => (x === null || x === undefined ? "-" : (x * 100).toFixed(d === undefined ? 1 : d) + "%");
  App.num = (x) => (x === null || x === undefined ? "-" : Number(x).toLocaleString());
  App.pageData = (name) => Number((App.$("#page") || {}).dataset[name] || 0);

  App.user = () => localStorage.getItem(KEY_USER) || "";
  App.toast = (msg, isError) => {
    const t = App.$("#toast"); if (!t) return;
    t.textContent = msg; t.className = "show" + (isError ? " error" : "");
    clearTimeout(App._toastTimer); App._toastTimer = setTimeout(() => (t.className = ""), 5000);
  };

  function headers(extra) {
    const h = Object.assign({}, extra || {});
    const user = App.user(), key = localStorage.getItem(KEY_API);
    if (user) h["X-User"] = user;
    if (key) h["X-API-Key"] = key;
    return h;
  }

  /* api(method, url, body, isForm): JSON in/out; rejects with Error{status, code, detail}. */
  App.api = async (method, url, body, isForm) => {
    const opts = { method, headers: headers(body && !isForm ? { "Content-Type": "application/json" } : {}) };
    if (body) opts.body = isForm ? body : JSON.stringify(body);
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      const detail = data && data.detail !== undefined ? data.detail : res.statusText;
      const err = new Error(typeof detail === "string" ? detail : (detail && detail.message) || JSON.stringify(detail));
      err.status = res.status; err.code = data && data.code; err.detail = detail;
      throw err;
    }
    return data;
  };
  App.run = async (fn) => {   // run an action, surface errors as a toast
    try { return await fn(); } catch (e) { App.toast((e.code ? e.code + ": " : "") + e.message, true); return null; }
  };

  App.download = async (url, filename) => {
    const res = await fetch(url, { headers: headers() });
    if (!res.ok) { App.toast("Download failed (" + res.status + ")", true); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob()); a.download = filename;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  /* Semantic badges: green > 85% / approved / validated, amber 60-85% / needs review,
     red < 60% / defect / blocked, purple = AI-suggested. */
  App.badge = (kind, text) => '<span class="badge ' + kind + '">' + App.esc(text) + "</span>";
  App.band = (x) => (x > 0.85 ? "green" : x >= 0.6 ? "amber" : "red");
  App.confBadge = (c) => (c === null || c === undefined ? App.badge("gray", "manual") : App.badge(App.band(c), Math.round(c * 100) + "%"));
  const STATUS = { APPROVED: "green", VALIDATED: "green", SUCCEEDED: "green", READY_FOR_LOAD: "green", PASS: "green", OPEN: "amber",
    SUBMITTED: "amber", RUNNING: "amber", DRAFT: "gray", REJECTED: "red", FAILED: "red", BLOCKED: "red", FAIL: "red" };
  App.statusBadge = (s) => App.badge(STATUS[String(s).toUpperCase()] || "gray", s);
  App.severityBadge = (s) => App.badge(s === "CRITICAL" ? "red" : s === "HIGH" ? "amber" : "gray", s);
  App.stat = (label, value) => '<div class="stat"><div class="label">' + App.esc(label) + '</div><div class="value">' + App.esc(value) + "</div></div>";
  // <meter> instead of an inline style: the CSP forbids style attributes.
  App.bar = (ratio) => '<meter min="0" max="1" value="' + App.esc(Math.max(0, Math.min(1, ratio))) + '" title="' + App.esc(App.pct(ratio)) + '"></meter>';
  /* table(headers, rows): rows are arrays of ready-made HTML strings (escaped by the caller). */
  App.table = (heads, rows, cls) => '<table><thead><tr>' + heads.map((h) => "<th>" + App.esc(h) + "</th>").join("") + "</tr></thead><tbody>" +
    (rows.length ? rows.map((r) => "<tr>" + r.map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("")
      : '<tr><td colspan="' + heads.length + '" class="muted">Nothing to show.</td></tr>') + "</tbody></table>";

  document.addEventListener("DOMContentLoaded", () => {
    const actor = App.$("#actor"), key = App.$("#apikey");
    if (actor) { actor.value = App.user(); actor.addEventListener("change", () => localStorage.setItem(KEY_USER, actor.value.trim())); }
    if (key) { key.value = localStorage.getItem(KEY_API) || ""; key.addEventListener("change", () => localStorage.setItem(KEY_API, key.value.trim())); }
  });
})();
