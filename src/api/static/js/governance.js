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

  // ---------------------------------------------------------------- what changed since an earlier run
  async function loadDeltaPanel() {
    const runs = ((await run(() => api("GET", "/api/runs"))) || []);
    const me = runs.find((r) => r.run_id === runId);
    const earlier = me ? runs.filter((r) => r.mapping_set_id === me.mapping_set_id && r.run_id < runId && r.quality_score !== null) : [];
    $("#delta-baseline").innerHTML = earlier.map((r) => '<option value="' + esc(r.run_id) + '">#' + esc(r.run_id) + " (" + esc(r.wave_name) + ")</option>").join("");
    $("#delta-compare").disabled = !earlier.length;
    $("#delta-baseline").disabled = !earlier.length;
    $("#delta-result").innerHTML = earlier.length ? "" : '<p class="muted">There is no earlier validated run of this mapping set to compare with.</p>';
    const saved = await api("GET", "/api/runs/" + runId + "/delta").catch(() => null);
    if (saved) { $("#delta-baseline").value = String(saved.baseline_run_id); renderDelta(saved); }
  }
  function renderDelta(d) {
    const c = d.counts;
    $("#delta-result").innerHTML = '<div class="cards">' + App.stat("New", num(c.new)) + App.stat("Changed", num(c.changed)) + App.stat("Unchanged", num(c.unchanged)) +
      App.stat("Removed", num(c.removed)) + "</div>" +
      '<p class="muted">' + num(c.current_valid) + " valid records now, " + num(c.baseline_valid) + " in run #" + esc(d.baseline_run_id) + "; compared on " + num(d.columns_compared) + " target fields." +
      (c.removed ? " Removed: " + Object.entries(d.removed_reasons).map(([k, v]) => esc(k.toLowerCase().replace(/_/g, " ")) + " " + num(v)).join(", ") + "." : "") + "</p>" +
      (Object.keys(d.examples).length ? '<p class="muted">' + Object.entries(d.examples).map(([k, v]) => esc(k.toLowerCase()) + ": " + v.map((x) => "<code>" + esc(x) + "</code>").join(", ")).join(" | ") + "</p>" : "");
  }
  $("#delta-compare").addEventListener("click", () => run(async () => {
    $("#delta-compare").disabled = true;
    try { renderDelta(await api("POST", "/api/runs/" + runId + "/delta", { baseline_run: Number($("#delta-baseline").value) })); }
    finally { $("#delta-compare").disabled = false; }
  }));
  const syncMerge = () => { $("#db-delete-wrap").classList.toggle("hidden", $("#db-mode").value !== "merge"); };
  $("#db-mode").addEventListener("change", syncMerge);

  // ---------------------------------------------------------------- deliver into a database target
  const dbBody = () => ({ connection: $("#db-conn").value, schema_name: $("#db-schema").value.trim() || undefined, table_name: $("#db-table").value.trim(),
    mode: $("#db-mode").value, include_source_columns: $("#db-src").checked,
    ...($("#db-mode").value === "merge" ? { baseline_run: Number($("#delta-baseline").value) || undefined, delete_removed: $("#db-delete").checked } : {}) });
  async function loadDbPanel() {
    const targets = (await run(() => api("GET", "/api/connections?role=target"))) || [];
    $("#db-conn").innerHTML = targets.map((t) => '<option value="' + esc(t.name) + '">' + esc(t.name + " (" + t.label + (t.default_schema ? ", " + t.default_schema : "") + ")") + "</option>").join("");
    const none = !targets.length;
    ["db-conn", "db-schema", "db-table", "db-mode", "db-src", "db-preview", "db-load"].forEach((id) => { $("#" + id).disabled = none; });
    $("#db-hint").textContent = none ? "No database target yet. Use New Target in the console to add an Oracle, SQL Server, PostgreSQL or MySQL target." :
      "Creates a new table in the chosen database with the validated records. Same gate as every load: steward sign-off and no open critical defects.";
    const done = (await run(() => api("GET", "/api/pipeline/load-db/" + runId))) || [];
    $("#db-history").innerHTML = done.length ? App.table(["Delivered", "Target", "Table", "Rows sent", "Rows counted in target", "Values", "By", ""], done.map((d, i) => {
      const last = d.verifications.length ? d.verifications[d.verifications.length - 1] : null;
      return [
        esc(d.loaded_at.replace("T", " ").slice(0, 19)), esc(d.connection + " (" + d.dialect + ")"), "<code>" + esc(d.schema_name + "." + d.table) + "</code>" + (d.replaced_existing ? " " + badge("amber", "replaced") : ""),
        d.mode === "merge" ? num(d.inserted_new) + " new, " + num(d.updated) + " replaced" + (d.deleted ? ", " + num(d.deleted) + " deleted" : "") + '<br><span class="muted">merge into run #' + esc(d.baseline_run) + "</span>" : num(d.rows_loaded),
        d.mode === "merge" ? num(d.rows_in_target) + " " + (d.rows_in_target === d.rows_before + d.inserted_new - d.deleted ? badge("green", "adds up") : badge("red", "differs"))
          : num(d.rows_in_target) + " " + (d.rows_in_target === d.valid_records ? badge("green", "matches") : badge("red", "differs")),
        (d.values_verified ? badge("green", num(d.cells_compared) + " cells checked") : badge("red", "not compared")) +
          (last ? "<br>" + (last.verified ? badge("green", "still identical") : badge("red", "changed: " + (last.differing_columns.join(", ") || "rows differ"))) +
            ' <span class="muted">' + esc(last.checked_at.replace("T", " ").slice(0, 16)) + "</span>" : ""),
        esc(d.loaded_by), d.rolled_back
          ? badge("amber", "rolled back by " + d.rolled_back.by) + ' <span class="muted">' + esc(d.rolled_back.reason) + "</span>"
          : '<button class="btn small db-verify" data-i="' + i + '" title="Compare what the table holds now with the validated payload, value by value">Verify now</button> ' +
            '<button class="btn small db-rollback" data-i="' + i + '" title="Delete exactly this run&#39;s rows from the table (and drop it if nothing else is in it)">Roll back</button>']; })) : "";
    $("#db-history").querySelectorAll(".db-rollback").forEach((btn) => btn.addEventListener("click", () => run(async () => {
      const d = done[Number(btn.dataset.i)];
      const reason = window.prompt("Roll back the delivery of " + num(d.rows_loaded) + " record(s) into " + d.schema_name + "." + d.table + "?\nExactly this run's rows are deleted" +
        (d.replaced_existing ? " (the table it replaced is already gone and cannot be restored)" : "") + ". Why? (kept in the record)");
      if (!reason || reason.trim().length < 3) return;
      btn.disabled = true;
      const r = await api("POST", "/api/pipeline/rollback-db/" + runId, { connection: d.connection, schema_name: d.schema_name, table_name: d.table, reason: reason.trim() });
      App.toast(r.rows_restored !== undefined && d.mode === "merge"
        ? num(r.rows_removed) + " row(s) written by this merge were removed and " + num(r.rows_restored) + " earlier row(s) put back; " + r.schema_name + "." + r.table + " holds " + num(r.rows_remaining) + " row(s) again"
        : num(r.rows_removed) + " row(s) removed from " + r.schema_name + "." + r.table + (r.table_dropped ? "; the table was empty, so it was dropped" : "; " + num(r.rows_remaining) + " row(s) of other runs stay"));
      await loadDbPanel(); load();
    })));
    $("#db-history").querySelectorAll(".db-verify").forEach((btn) => btn.addEventListener("click", () => run(async () => {
      const d = done[Number(btn.dataset.i)];
      btn.disabled = true; App.toast("Comparing every value in " + d.schema_name + "." + d.table + " with the validated payload...");
      const r = await api("POST", "/api/pipeline/verify-db/" + runId, { connection: d.connection, schema_name: d.schema_name, table_name: d.table });
      App.toast(r.verified ? num(r.cells_compared) + " cell(s) compared: the table is identical to the validated payload"
        : "The table has changed: " + (r.differing_columns.join(", ") || "its row count differs") + " (" + num(r.rows_in_target) + " row(s) in the table, " + num(r.rows_in_payload) + " in the payload)", !r.verified);
      await loadDbPanel(); load();
    })));
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
  loadDeltaPanel();

  App.reloadGovernance = load;
  load();
})();
