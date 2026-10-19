(function () {
  "use strict";
  const { $, api, run, stat, num } = App;
  (async () => {
    const [tables, sets, runs] = await Promise.all([
      run(() => api("GET", "/api/tables")), run(() => api("GET", "/api/mapping-sets")), run(() => api("GET", "/api/runs"))]);
    if (!tables) { $("#stats").innerHTML = '<p class="muted">Enter your name in <em>Acting as</em> (top right) to load your workspace.</p>'; return; }
    const approved = (sets || []).filter((s) => s.status === "APPROVED").length;
    const latest = (runs || [])[0];
    $("#stats").innerHTML = [
      stat("Sources registered", num(tables.length)), stat("Mapping sets", num((sets || []).length) + (approved ? " (" + approved + " approved)" : "")),
      stat("Pipeline runs", num((runs || []).length)), stat("Latest run", latest ? "#" + latest.run_id + " " + latest.status.toLowerCase() : "none yet"),
    ].join("");
  })();
})();
