(function () {
  "use strict";
  const { $, esc, api, run, stat, num, badge } = App;
  // The start page lives in a tab of the console; the Add Source wizard and the tabs belong to the console around it.
  const consoleApp = () => (window.parent !== window ? window.parent : null);
  const wizards = () => { const c = consoleApp(); return c && c.Wizards ? c.Wizards : null; };

  function openTab(url) {
    const c = consoleApp();
    if (c && c.Shell && c.Shell.openTab) c.Shell.openTab(url); else window.location.href = url;
  }
  function act(a) {
    if (a.do === "open_tab") return openTab(a.arg);
    if (a.do === "open_wizard") {
      let fill = null;
      try { fill = a.arg ? JSON.parse(a.arg) : null; } catch (e) { fill = null; }
      const w = wizards();
      if (w) return w.openSourceWith(fill, null);
      App.toast("Open the console to add a source.", true);
    }
  }

  const guide = window.Guide ? Guide.mount($("#start-guide"), {
    title: "Guide", context: () => ({ step: "start" }),
    onAction: act,
    onFile: (file) => { const w = wizards(); if (w) w.openSourceWith(null, file); else guide.say("<p>Open the console, then drop the file here again.</p>"); },
  }) : null;

  async function journey() {
    const p = await api("GET", "/api/guide/progress").catch(() => null);
    if (!p) return;
    $("#journey").innerHTML = p.steps.map((s, i) => '<li class="' + s.status + '"><span class="n">' + (s.status === "done" ? "&#10003;" : i + 1) + "</span><span class=\"t\">" + esc(s.label) +
      (s.detail ? ' <span class="muted">' + esc(s.detail) + "</span>" : "") + "</span></li>").join("");
    $("#journey-now").innerHTML = "<strong>Now:</strong> " + esc(p.headline);
    $("#journey-actions").innerHTML = p.actions.map((a, i) => '<button type="button" class="btn primary journey-act" data-i="' + i + '">' + esc(a.label) + "</button>").join(" ");
    $("#journey-actions").querySelectorAll(".journey-act").forEach((b) => b.addEventListener("click", () => act(p.actions[Number(b.dataset.i)])));
  }

  async function stats() {
    const [tables, sets, runs] = await Promise.all([
      run(() => api("GET", "/api/tables")), run(() => api("GET", "/api/mapping-sets")), run(() => api("GET", "/api/runs"))]);
    if (!tables) { $("#stats").innerHTML = '<p class="muted">Enter your name in <em>Acting as</em> (top right) to load your workspace.</p>'; return; }
    const approved = (sets || []).filter((s) => s.status === "APPROVED").length;
    const latest = (runs || [])[0];
    $("#stats").innerHTML = [
      stat("Sources registered", num(tables.length)), stat("Mapping sets", num((sets || []).length) + (approved ? " (" + approved + " approved)" : "")),
      stat("Pipeline runs", num((runs || []).length)), stat("Latest run", latest ? "#" + latest.run_id + " " + latest.status.toLowerCase() : "none yet"),
    ].join("");
  }

  const refresh = () => Promise.all([journey(), stats()]);
  refresh();
  if (guide) guide.refresh(true);
  window.addEventListener("focus", refresh);                       // coming back from the wizard or another tab: the stages may have moved on
  setInterval(refresh, 20000);
})();
