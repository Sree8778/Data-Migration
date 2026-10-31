/* The setup guide: a chat that sits beside the step the person is doing.
   It says what the step needs, answers questions about it, explains what went wrong and what to do, fills the form in from what the person types (never a password), and
   takes a dropped file. Used beside the Add Source wizard and on the start page.
   Guide.mount(el, { context: () => ({step, kind, fields, tables, error, file_name}), onFill(fill), onFile(file), onAction(action), title }) -> { refresh(), ask(text) } */
(function () {
  "use strict";
  const { esc, api, badge } = App;
  const MAX_LOG = 40;
  // a password typed into the chat is never shown back, not even on this screen
  const hideSecrets = (t) => t.replace(/\b(password|passwd|pwd|passcode)\b(\s*(?:is|=|:)?\s*)\S+/gi, "$1$2[hidden]");

  function mount(el, opts) {
    el.classList.add("guide");
    el.innerHTML = '<div class="guide-head"><strong>' + esc(opts.title || "Setup guide") + '</strong><span class="muted">I explain each step, and fill in what you tell me</span></div>' +
      '<div class="guide-need hidden"></div><div class="guide-log" role="log" aria-live="polite"></div><div class="guide-chips"></div>' +
      '<form class="guide-input" autocomplete="off"><button type="button" class="guide-attach" title="Attach a file (or drop one on this panel)" aria-label="Attach a file">+</button>' +
      '<input type="file" class="guide-file hidden" accept=".csv,.tsv,.txt,.psv,.dat,.xlsx,.json,.ndjson,.jsonl,.parquet,.gz,.zip">' +
      '<input type="text" class="guide-text" maxlength="600" placeholder="Ask a question, or tell me the server, database and user..." aria-label="Message to the guide">' +
      '<button type="submit" class="guide-send">Send</button></form>';
    const log = el.querySelector(".guide-log"), need = el.querySelector(".guide-need"), chips = el.querySelector(".guide-chips");
    const text = el.querySelector(".guide-text"), fileInput = el.querySelector(".guide-file");
    let lastStep = null, busy = false;

    const add = (who, html) => {
      log.insertAdjacentHTML("beforeend", '<div class="msg ' + who + '">' + html + "</div>");
      while (log.children.length > MAX_LOG) log.firstElementChild.remove();
      log.scrollTop = log.scrollHeight;
    };

    function show(r) {
      need.classList.toggle("hidden", !r.required_now.length);
      need.innerHTML = r.required_now.length ? "<strong>This step needs</strong><ul>" + r.required_now.map((w) => "<li" + (r.still_missing.includes(w) ? ' class="missing"' : ' class="held"') + ">" + esc(w) + "</li>").join("") + "</ul>" : "";
      let html = "<p>" + esc(r.reply) + "</p>";
      if (r.warning) html += '<p class="guide-warn">' + esc(r.warning) + "</p>";
      if (r.actions && r.actions.length) html += '<p class="guide-actions">' + r.actions.map((a, i) => '<button type="button" class="btn small primary guide-act" data-i="' + i + '">' + esc(a.label) + "</button>").join(" ") + "</p>";
      (r.held_back || []).forEach((h, i) => {
        html += '<div class="guide-held"><p><strong>Not filled in:</strong> ' + esc(h.field) + " = " + esc(h.value) + "</p><p>" + esc(h.why) + "</p>" + (h.suggestion ? "<p>" + esc(h.suggestion) + "</p>" : "") +
          '<p><button type="button" class="btn small guide-use" data-i="' + i + '">I am sure, use it anyway</button> <button type="button" class="btn small guide-keep">No, I will change it</button></p></div>';
      });
      add("bot", html);
      log.querySelectorAll(".guide-use:not([data-bound])").forEach((b) => { b.dataset.bound = "1"; b.addEventListener("click", () => {
        const h = r.held_back[Number(b.dataset.i)];
        if (opts.onFill) opts.onFill({ [h.field]: h.value }, r);
        b.closest(".guide-held").innerHTML = "<p>Done: I put " + esc(h.field) + " = " + esc(h.value) + " into the form, as you decided.</p>";
      }); });
      log.querySelectorAll(".guide-keep:not([data-bound])").forEach((b) => { b.dataset.bound = "1"; b.addEventListener("click", () => { b.closest(".guide-held").innerHTML = "<p>All right, I left it out. Tell me the right value when you have it.</p>"; }); });
      log.querySelectorAll(".guide-act:not([data-bound])").forEach((b) => { b.dataset.bound = "1"; b.addEventListener("click", () => opts.onAction && opts.onAction(r.actions[Number(b.dataset.i)], r)); });
      chips.innerHTML = (r.suggestions || []).map((s) => '<button type="button" class="chip" data-q="' + esc(s) + '">' + esc(s) + "</button>").join("");
      chips.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => ask(c.dataset.q)));
      if (r.fill && Object.keys(r.fill).length && opts.onFill) opts.onFill(r.fill, r);
    }

    async function call(message) {
      if (busy) return;
      busy = true;
      const body = Object.assign({ step: "start", kind: "", fields: {}, tables: [], error: "", file_name: "" }, opts.context ? opts.context() : {}, { message: message || "" });
      delete body.key;                                    // the client's own marker for "which step is this", not part of the request
      body.fields = Object.fromEntries(Object.entries(body.fields || {}).filter(([k, v]) => v && k !== "password"));
      try { show(await api("POST", "/api/guide/chat", body)); }
      catch (e) { add("bot", '<p class="guide-warn">' + esc(e.message) + "</p>"); }
      finally { busy = false; }
    }

    function ask(message) {
      const clean = (message || "").trim();
      if (!clean) return;
      add("user", "<p>" + esc(hideSecrets(clean)) + "</p>");
      text.value = "";
      return call(clean);
    }

    function refresh(force) {
      const c = opts.context ? opts.context() : { step: "start" }, key = c.key || c.step;
      if (!force && key === lastStep) return Promise.resolve();
      lastStep = key;
      return call("");
    }
    function reset() { log.innerHTML = ""; chips.innerHTML = ""; need.innerHTML = ""; need.classList.add("hidden"); lastStep = null; }

    el.querySelector(".guide-input").addEventListener("submit", (e) => { e.preventDefault(); ask(text.value); });
    el.querySelector(".guide-attach").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => { if (fileInput.files[0]) takeFile(fileInput.files[0]); fileInput.value = ""; });
    function takeFile(file) {
      add("user", "<p>&#128206; " + esc(file.name) + "</p>");
      if (opts.onFile) opts.onFile(file);
      else add("bot", "<p>Open Add Source to bring this file in.</p>");
    }
    ["dragenter", "dragover"].forEach((ev) => el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.add("drop"); }));
    ["dragleave", "drop"].forEach((ev) => el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.remove("drop"); }));
    el.addEventListener("drop", (e) => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) takeFile(f); });

    return { refresh, ask, reset, say: (html) => add("bot", html), forceRefresh: () => refresh(true) };
  }
  window.Guide = { mount };
})();
