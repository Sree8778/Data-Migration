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

  /* Inside the management console every page is a tab: links to other pages open a new tab and
     changes made here ask the console to refresh its explorer. Stand-alone pages behave as before. */
  App.embedded = window.parent !== window;
  App.open = (url, title) => {
    if (App.embedded) window.parent.postMessage({ type: "open-tab", url, title }, window.location.origin);
    else window.location.href = url;
  };
  App.changed = () => { if (App.embedded) window.parent.postMessage({ type: "changed" }, window.location.origin); };
  document.addEventListener("click", (e) => {
    const a = App.embedded && e.target.closest && e.target.closest("a[href^='/ui/']");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault(); App.open(a.getAttribute("href"), (a.dataset.title || a.textContent).trim());
  });

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
    if (method !== "GET") App.changed();
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

  /* Proposals that wait for the person's approval. Each one says what is wrong, what is proposed, exactly what will change and what will not, how to undo it, and asks.
     Nothing is done until "Yes, go ahead"; "No, not now" changes nothing and is recorded. A decision goes to the server with approved = true / false.
     fixes: [{kind, title, situation, proposal, what_happens[], unchanged[], undo, question, mapping_set_id, rule_id, target, inputs[{name,label,suggestions[],hint}], editable_now}] */
  App.renderFixes = (el, fixes, afterFix) => {
    const { esc, api, run, badge } = App;
    if (!fixes || !fixes.length) { el.innerHTML = ""; return; }
    el.innerHTML = '<h2>Needs your approval</h2><p class="muted">I have not changed anything. Below, each proposal says what I found and what I would do. Read it, then tell me yes or no: nothing happens until you approve.</p>' +
      fixes.map((f, i) => '<div class="card fix approval" data-i="' + i + '"><h3>' + esc(f.title) + "</h3>" +
        (f.situation ? '<p><strong>The situation.</strong> ' + esc(f.situation) + "</p>" : "") +
        (f.proposal ? '<p><strong>What I propose.</strong> ' + esc(f.proposal) + "</p>" : "") +
        '<details><summary>Exactly what will change, what will not, and how to undo it</summary><p class="fix-label">What will change</p><ul>' + f.what_happens.map((t) => "<li>" + esc(t) + "</li>").join("") + "</ul>" +
        (f.unchanged.length ? '<p class="fix-label">What will not change</p><ul>' + f.unchanged.map((t) => "<li>" + esc(t) + "</li>").join("") + "</ul>" : "") +
        (f.undo ? '<p class="fix-label">How to undo it</p><p>' + esc(f.undo) + "</p>" : "") + "</details>" +
        f.inputs.map((x, j) => '<label class="fix-input">' + esc(x.label) + ' <input class="fix-value" data-name="' + esc(x.name) + '" list="fixlist-' + i + "-" + j + '" autocomplete="off" placeholder="' + esc(x.hint) + '">' +
          '<datalist id="fixlist-' + i + "-" + j + '">' + x.suggestions.map((v) => '<option value="' + esc(v) + '">').join("") + "</datalist></label>").join("") +
        '<p class="fix-question"><strong>' + esc(f.question || "Do you approve?") + "</strong></p>" +
        '<p><button class="btn primary fix-yes" type="button">Yes, go ahead</button> <button class="btn fix-no" type="button">No, not now</button> <span class="fix-copy">' +
        (f.editable_now ? "" : badge("amber", "goes into a new editable copy")) + '</span></p><div class="fix-result"></div></div>').join("");
    el.querySelectorAll(".fix").forEach((card) => {
      const f = fixes[Number(card.dataset.i)], yes = card.querySelector(".fix-yes"), no = card.querySelector(".fix-no"), out = card.querySelector(".fix-result");
      const send = (approved, override) => {
        const values = {};
        let missing = false;
        card.querySelectorAll(".fix-value").forEach((x) => { values[x.dataset.name] = x.value.trim(); if (!x.value.trim()) missing = true; });
        if (approved && missing) { App.toast("Tell me the value first, then approve", true); return; }
        run(async () => {
          yes.disabled = no.disabled = true;
          out.innerHTML = approved ? '<p class="muted"><span class="spinner"></span> Approved. Working on it...</p>' : "";
          try {
            const r = await api("POST", "/api/mapping/fix", { kind: f.kind, mapping_set_id: f.mapping_set_id, rule_id: f.rule_id, target: f.target, values, approved, shown: f.title, override: !!override });
            if (r.challenge) {                                  // the application disagrees: it did not do it, and says why
              const c = r.challenge, warn = c.level === "warn";
              out.innerHTML = '<div class="pushback ' + c.level + '"><p><strong>' + (warn ? "I disagree, so I have not done it yet." : "I will not do this.") + "</strong> " + esc(c.reason) + "</p>" +
                "<p><strong>Why it matters.</strong> " + esc(c.why) + "</p>" + (c.suggestion ? "<p><strong>What I would do instead.</strong> " + esc(c.suggestion) + "</p>" : "") +
                (warn ? '<p><button class="btn fix-insist" type="button">I have read that and still want it</button> <button class="btn fix-change" type="button">No, I will change my answer</button></p>' : "") + "</div>";
              yes.disabled = no.disabled = false;
              const insist = out.querySelector(".fix-insist"), change = out.querySelector(".fix-change");
              if (insist) insist.addEventListener("click", () => send(true, true));
              if (change) change.addEventListener("click", () => { out.innerHTML = ""; const v = card.querySelector(".fix-value"); if (v) v.focus(); });
              App.toast("I disagree with that: " + c.reason, true);
              return;
            }
            if (r.declined) { out.innerHTML = '<p>' + badge("gray", "not approved") + " <strong>Understood. I changed nothing</strong> and will not do this unless you ask again. Your decision is on the audit trail.</p>"; return; }
            out.innerHTML = "<p>" + badge(r.ok ? "green" : "amber", r.ok ? "done, as you approved" : "nothing was changed") + " <strong>" + esc(r.message) + "</strong></p>" +
              (r.changes.length ? '<ul class="findings">' + r.changes.map((c) => "<li>" + esc(c) + "</li>").join("") + "</ul>" : "") +
              (r.needs_decision.length ? "<p><strong>Left for you to decide:</strong></p><ul class=\"findings\">" + r.needs_decision.map((c) => "<li>" + esc(c) + "</li>").join("") + "</ul>" : "") +
              (r.next_step ? '<p class="muted">Next: ' + esc(r.next_step) + "</p>" : "");
            if (!r.ok) { yes.disabled = no.disabled = false; }
            if (r.copied_from) {                                  // the first fix made the draft copy: the others go into it, not into another copy
              fixes.forEach((x, k) => { if (x.mapping_set_id === r.copied_from) { x.mapping_set_id = r.mapping_set_id; x.editable_now = true; el.querySelectorAll(".fix")[k].querySelector(".fix-copy").innerHTML = badge("green", "adds to draft #" + r.mapping_set_id); } });
            }
            App.toast(r.message, !r.ok);
            if (afterFix) await afterFix(r);
          } catch (e) { out.innerHTML = ""; yes.disabled = no.disabled = false; throw e; }
        });
      };
      yes.addEventListener("click", () => send(true));
      no.addEventListener("click", () => send(false));
    });
  };

  document.addEventListener("DOMContentLoaded", () => {
    const actor = App.$("#actor"), key = App.$("#apikey");
    if (actor) { actor.value = App.user(); actor.addEventListener("change", () => localStorage.setItem(KEY_USER, actor.value.trim())); }
    if (key) { key.value = localStorage.getItem(KEY_API) || ""; key.addEventListener("change", () => localStorage.setItem(KEY_API, key.value.trim())); }
  });
})();
