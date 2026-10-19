(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, severityBadge, statusBadge } = App;
  const runId = App.pageData("runId");

  function renderGate(r) {
    const ready = r.state === "READY_FOR_LOAD";
    $("#gate").className = "banner " + (ready ? "green" : "red");
    $("#gate").innerHTML = "<strong>" + (ready ? "READY FOR LOAD" : "BLOCKED") + "</strong> - mapping " + esc(r.mapping_status) +
      ", run " + esc(r.run_status) + ", validated: " + (r.validated ? "yes" : "no") + ", open CRITICAL defects: " + esc(r.open_critical_defects) +
      (r.reasons.length ? "<ul>" + r.reasons.map((x) => "<li>" + esc(x) + "</li>").join("") + "</ul>" : "");
  }

  async function load() {
    const d = await run(() => api("GET", "/api/defects/" + runId));
    if (!d) return;
    renderGate(d.readiness);
    $("#sync-jira").textContent = "Sync defects to Jira" + (d.jira_mode === "dry-run" ? " (dry-run)" : "");
    $("#defects").innerHTML = '<p class="muted">Quality score: <strong>' + esc(d.quality_score === null ? "n/a" : d.quality_score + "%") +
      "</strong> | open defects: " + esc(d.open_defects) + " (critical " + esc(d.open_critical) + ")</p>" +
      table(["Severity", "Rule signature", "Target field", "Records", "Sample failing IDs", "Root cause", "Jira"],
        d.defects.map((x) => [
          severityBadge(x.severity), "<code>" + esc(x.rule_signature) + "</code>", esc(x.target),
          x.is_open ? "<strong>" + num(x.violation_count) + "</strong>" : badge("green", "resolved"),
          "<span class='mono'>" + esc(x.sample_failing_record_ids.join(", ")) + "</span>", esc(x.root_cause),
          x.jira_issue_key ? esc(x.jira_issue_key) + " " + statusBadge(x.jira_status || "OPEN") : '<span class="muted">not synced</span>',
        ]));
  }

  $("#sync-jira").addEventListener("click", () => run(async () => {
    const r = await api("POST", "/api/defects/" + runId + "/sync-jira");
    App.toast((r.dry_run ? "Dry-run: " : "") + r.issue_keys.length + " issue(s) " + (r.dry_run ? "would be synced" : "synced") +
      (r.errors.length ? "; " + r.errors.length + " error(s)" : ""), r.errors.length > 0);
    load();
  }));

  $("#sign-off").addEventListener("click", () => run(async () => {
    const r = await api("POST", "/api/governance/sign-off-load/" + runId);
    App.toast("Load signed off by " + r.signed_off_by); load();
  }));

  $("#load").addEventListener("click", () => run(async () => {
    const [mode, target] = $("#load-mode").value.split(":");
    const r = await api("POST", "/api/pipeline/load/" + runId, { mode, target: target || "sandbox" });
    if (r.mode === "cockpit") { App.toast("Cockpit workbook generated"); await App.download(r.download_url, "migration_cockpit_run_" + runId + ".xlsx"); }
    else App.toast((r.simulated ? "Simulated SAP load: " : "SAP load: ") + r.load.loaded + " loaded, " + r.load.failed + " failed", r.load.failed > 0);
    load();
  }));

  // ---------------------------------------------------------------- deliver into a database target
  const dbBody = () => ({ connection: $("#db-conn").value, schema_name: $("#db-schema").value.trim() || undefined, table_name: $("#db-table").value.trim(),
    mode: $("#db-mode").value, include_source_columns: $("#db-src").checked });
  async function loadDbPanel() {
    const targets = (await run(() => api("GET", "/api/connections?role=target"))) || [];
    $("#db-conn").innerHTML = targets.map((t) => '<option value="' + esc(t.name) + '">' + esc(t.name + " (" + t.label + (t.default_schema ? ", " + t.default_schema : "") + ")") + "</option>").join("");
    const none = !targets.length;
    ["db-conn", "db-schema", "db-table", "db-mode", "db-src", "db-preview", "db-load"].forEach((id) => { $("#" + id).disabled = none; });
    $("#db-hint").textContent = none ? "No database target yet. Use New Target in the console to add an Oracle, SQL Server, PostgreSQL or MySQL target." :
      "Creates a new table in the chosen database with the validated records. Same gate as every load: steward sign-off and no open critical defects.";
    const done = (await run(() => api("GET", "/api/pipeline/load-db/" + runId))) || [];
    $("#db-history").innerHTML = done.length ? App.table(["Delivered", "Target", "Table", "Rows sent", "Rows counted in target", "By"], done.map((d) => [
      esc(d.loaded_at.replace("T", " ").slice(0, 19)), esc(d.connection + " (" + d.dialect + ")"), "<code>" + esc(d.schema_name + "." + d.table) + "</code>" + (d.replaced_existing ? " " + badge("amber", "replaced") : ""),
      num(d.rows_loaded), num(d.rows_in_target) + " " + (d.rows_in_target === d.valid_records ? badge("green", "matches") : badge("red", "differs")), esc(d.loaded_by)])) : "";
  }
  $("#db-preview").addEventListener("click", () => run(async () => {
    const b = dbBody();
    const q = new URLSearchParams({ connection: b.connection, table_name: b.table_name, include_source_columns: String(b.include_source_columns) });
    if (b.schema_name) q.set("schema_name", b.schema_name);
    const p = await api("GET", "/api/pipeline/load-db/" + runId + "/preview?" + q.toString());
    $("#db-ddl").textContent = "-- " + p.rows + " row(s) into " + p.schema_name + "." + p.table + "\n" + p.ddl; $("#db-ddl").classList.remove("hidden");
  }));
  $("#db-load").addEventListener("click", () => run(async () => {
    const r = await api("POST", "/api/pipeline/load-db/" + runId, dbBody());
    App.toast(num(r.rows_loaded) + " record(s) delivered to " + r.schema_name + "." + r.table + " (" + r.dialect + "); " + num(r.rows_in_target) + " counted in the target");
    $("#db-ddl").classList.add("hidden"); loadDbPanel(); load();
  }));
  loadDbPanel();

  App.reloadGovernance = load;
  load();
})();
