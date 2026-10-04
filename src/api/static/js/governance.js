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

  load();
})();
