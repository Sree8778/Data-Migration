(function () {
  "use strict";
  const { $, esc, api, run, table, num, statusBadge, badge } = App;

  async function loadHealth() {
    const h = await run(() => api("GET", "/api/health"));
    if (!h) return;
    const el = $("#status-banner");
    const bits = ["Database: " + h.database, "Auth: " + h.auth_mode, "Jira: " + h.jira_mode, "SAP target: " + h.sap_default_target + " (simulated)"];
    el.className = "banner " + (h.database !== "ok" ? "red" : !h.target_catalog_ready ? "amber" : "");
    el.innerHTML = esc(bits.join("  |  ")) + (h.target_catalog_ready ? "" :
      ' <button class="btn small" id="seed">Seed SAP target catalog</button>');
    if (h.auth_mode === "dev-header") el.innerHTML += ' <span class="muted">(identity from the "Acting as" field)</span>';
    const seed = $("#seed");
    if (seed) seed.onclick = () => run(async () => { await api("POST", "/api/admin/seed-sap-catalog"); App.toast("Target catalog seeded"); loadHealth(); });
  }

  async function loadTables() {
    const tables = await run(() => api("GET", "/api/tables")) || [];
    $("#tables").innerHTML = table(["Table", "System", "Columns", "Rows", "Last profiled", "File", ""],
      tables.map((t) => [
        "<strong>" + esc(t.table_name) + "</strong> <span class='muted'>#" + esc(t.table_id) + "</span>", esc(t.system_name),
        num(t.column_count), num(t.row_count_estimate), esc(t.last_profiled_at ? t.last_profiled_at.replace("T", " ").slice(0, 19) : "never"),
        t.has_source_file ? badge("green", "uploaded") : badge("gray", "none"),
        '<a class="btn small" href="/ui/profiler/' + esc(t.table_id) + '">Profiler</a> <a class="btn small" href="/ui/studio/' + esc(t.table_id) + '">Mapping Studio</a>',
      ]));
  }

  async function loadRuns() {
    const runs = await run(() => api("GET", "/api/runs")) || [];
    $("#runs").innerHTML = table(["Run", "Wave", "Mapping set", "Status", "Phase", "Extracted", "Loaded", "Failed", "Quality", ""],
      runs.map((r) => [
        "#" + esc(r.run_id), esc(r.wave_name), esc(r.mapping_set_id), statusBadge(r.status), esc(r.phase),
        num(r.records_extracted), num(r.records_loaded), num(r.records_failed),
        r.quality_score === null ? "-" : esc(r.quality_score.toFixed(1)) + "%",
        '<a class="btn small" href="/ui/governance/' + esc(r.run_id) + '">Governance</a> <a class="btn small" href="/ui/certificate/' + esc(r.run_id) + '">Certificate</a>',
      ]));
  }

  $("#register-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const r = await api("POST", "/api/sources/register", new FormData(ev.target), true);
      App.toast("Registered table #" + r.table_id + " (" + r.columns_registered + " columns)");
      ev.target.reset(); loadTables();
    });
  });

  loadHealth(); loadTables(); loadRuns();
})();
