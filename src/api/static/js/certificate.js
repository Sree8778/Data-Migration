(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, statusBadge } = App;
  const runId = App.pageData("runId");

  function render(r) {
    const rep = r.report, c = rep.counts;
    $("#verdict").className = "banner " + (r.passed ? "green" : "red");
    $("#verdict").innerHTML = "<strong>" + (r.passed ? "RECONCILIATION PASSED" : "NOT PASSED") + "</strong> - variance " + esc(rep.variance) +
      " row(s), " + (rep.complete ? "complete" : "INCOMPLETE (no validation report)") + ", run " + esc(rep.run_status) + " / " + esc(rep.run_phase);

    $("#counts").innerHTML = [["Extracted", c.extracted], ["Transformed OK", c.transformed_ok], ["Duplicate children", c.duplicate_children],
      ["Rejected by validation", c.validation_rejected], ["Valid for load", c.valid], ["Loaded in SAP", c.loaded], ["Failed in SAP", c.load_failed]]
      .map(([l, v]) => App.stat(l, v === null ? "n/a" : num(v))).join("");

    $("#dropoff").innerHTML = table(["Reason", "Records", "Explanation", "Sample source keys"], rep.dropoff.map((d) => [
      "<strong>" + esc(d.reason) + "</strong>", num(d.count), esc(d.explanation), "<span class='mono'>" + esc(d.sample_pks.join(", ")) + "</span>"])) +
      "<p class='muted'>Accounted rows: " + num(rep.accounted_rows) + " of " + num(c.extracted) + " extracted.</p>";

    $("#checks").innerHTML = table(["Result", "Control", "Detail"], rep.checks.map((k) => [
      statusBadge(k.passed ? "PASS" : "FAIL"), "<code>" + esc(k.name) + "</code>", esc(k.detail)]));

    const l = rep.lineage_sample;
    $("#lineage").innerHTML = "<p>" + esc(l.passed) + " of " + esc(l.sampled) + " sampled loaded records verified end to end.</p><ul>" +
      l.examples.map((x) => "<li class='mono'>" + esc(x) + "</li>").join("") +
      l.failures.map((x) => "<li class='mono'>" + badge("red", "FAIL") + " " + esc(x) + "</li>").join("") + "</ul>";

    $("#approval").innerHTML = table(["Mapping set", "Created by", "Approved by", "Approved at", "Open CRITICAL defects"], [[
      esc(rep.mapping_set_id) + " (v" + esc(rep.mapping_version) + ")", esc(rep.mapping_created_by), esc(rep.mapping_approved_by || "-"),
      esc(rep.mapping_approved_at ? rep.mapping_approved_at.replace("T", " ").slice(0, 19) : "-"), esc(rep.open_critical_defects)]]);

    const bind = (id, url, name) => { const b = $(id); b.disabled = !url; b.onclick = () => App.download(url, name); };
    bind("#dl-cockpit", r.cockpit_download_url, "migration_cockpit_run_" + runId + ".xlsx");
    bind("#dl-html", r.export_urls.html, "reconciliation_run_" + runId + ".html");
    bind("#dl-md", r.export_urls.markdown, "reconciliation_run_" + runId + ".md");
  }

  run(async () => render(await api("GET", "/api/reconciliation/" + runId)));
})();
