/* Data Construction tab of the Governance Hub: template download, upload, re-validate, activity log. */
(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, statusBadge, stat } = App;
  const runId = App.pageData("runId");
  let loaded = false;

  // ---------------------------------------------------------------- tabs
  const tabs = { defects: ["#tab-defects", "#panel-defects"], construction: ["#tab-construction", "#panel-construction"] };
  function showTab(name) {
    Object.keys(tabs).forEach((key) => {
      const on = key === name;
      $(tabs[key][0]).classList.toggle("active", on);
      $(tabs[key][0]).setAttribute("aria-selected", String(on));
      $(tabs[key][1]).classList.toggle("hidden", !on);
    });
    if (name === "construction" && !loaded) { loaded = true; load(); }
  }
  Object.keys(tabs).forEach((key) => $(tabs[key][0]).addEventListener("click", () => showTab(key)));

  // ---------------------------------------------------------------- summary
  const STATUS_TEXT = {
    NOT_REQUIRED: ["green", "No construction needed"], PENDING: ["amber", "Data needed"],
    REVALIDATION_REQUIRED: ["amber", "Re-validation required"], LOCKED: ["red", "Locked"],
  };

  async function load() {
    const s = await run(() => api("GET", "/api/construction/" + runId));
    if (!s) return;
    const [kind, label] = STATUS_TEXT[s.status] || ["gray", s.status];
    $("#construction-cards").innerHTML = [
      stat("Total incomplete records", num(s.incomplete_records)),
      stat("Required target fields", num(s.required_fields.length)),
      '<div class="stat"><div class="label">Status</div><div class="value">' + badge(kind, label) + "</div></div>",
    ].join("");

    const banner = $("#construction-banner");
    const showBanner = !s.can_construct || s.status === "REVALIDATION_REQUIRED";
    banner.className = "banner " + (s.can_construct ? "amber" : "red") + (showBanner ? "" : " hidden");
    banner.textContent = s.can_construct ? "Values were merged. Re-validate to refresh the defects and the load gate." : s.reason || "";

    $("#construction-fields").innerHTML = table(["SAP target field", "Records missing a value", "Type"],
      s.required_fields.map((f) => ["<strong>" + esc(f.target) + "</strong>", num(f.records), esc(f.kind)]));

    $("#download-template").disabled = !s.can_construct || s.pending_cells === 0;
    $("#construction-file").disabled = !s.can_construct;
    $("#revalidate").disabled = !s.can_construct;

    $("#construction-log").innerHTML = table(["When (UTC)", "Who", "Event", "Details"], s.activity.slice().reverse().map((a) => [
      esc(a.at.replace("T", " ").slice(0, 19)), "<strong>" + esc(a.user) + "</strong>", statusBadge(a.event.toUpperCase()), esc(describe(a))]));
  }

  function describe(a) {
    const d = a.detail || {};
    if (a.event === "upload") return (d.filename || "file") + ": " + num(d.records_updated) + " records fixed, " + num(d.cells_modified) +
      " cells merged" + (d.cells_rejected ? ", " + num(d.cells_rejected) + " rejected" : "") + " (" + d.status + ")";
    if (a.event === "revalidate") return "quality " + d.quality_score + "%, " + num(d.open_defects) + " open defects, gate " + d.gate +
      (d.signoff_revoked ? ", sign-off revoked" : "");
    return num(d.records) + " records, fields: " + (d.fields || []).join(", ");
  }

  // ---------------------------------------------------------------- download
  $("#download-template").addEventListener("click", () => run(async () => {
    await App.download("/api/construction/template/" + runId, "construction_template_run_" + runId + ".xlsx");
    App.toast("Template downloaded - fill the amber cells and upload it");
    load();
  }));

  // ---------------------------------------------------------------- upload
  function renderResult(r) {
    const kind = r.validation_status === "OK" ? "green" : r.validation_status === "PARTIAL" ? "amber" : "red";
    let html = '<div class="banner ' + kind + '"><strong>' + esc(r.validation_status) + "</strong> - " + num(r.records_updated) + " records updated, " +
      num(r.cells_modified) + " cells merged, " + num(r.cells_rejected) + " rejected, " + num(r.remaining_cells_pending) + " still empty (lineage rows: " + num(r.lineage_rows) + ")</div>";
    if (r.rejected_samples.length) {
      html += table(["Row", "SOURCE_PK", "Field", "Value", "Why it was rejected"], r.rejected_samples.map((x) => [
        esc(x.row_ref), esc(x.source_pk), esc(x.field), "<code>" + esc(x.value) + "</code>", esc(x.reason)]));
    }
    $("#upload-result").innerHTML = html;
  }

  async function upload(file) {
    if (!file) return;
    if (!/\.xlsx$/i.test(file.name)) { App.toast("Please upload the completed .xlsx template", true); return; }
    const zone = $("#dropzone"); zone.classList.add("busy");
    try {
      const form = new FormData(); form.append("file", file);
      const r = await api("POST", "/api/construction/upload/" + runId, form, true);
      renderResult(r);
      App.toast(num(r.cells_modified) + " cells merged in " + num(r.records_updated) + " records" + (r.cells_rejected ? "; " + r.cells_rejected + " rejected" : ""), r.validation_status !== "OK");
      if (r.cells_modified > 0 && $("#auto-revalidate").checked) await revalidate(); else await load();
    } catch (e) {
      App.toast((e.code ? e.code + ": " : "") + e.message, true);
      $("#upload-result").innerHTML = '<div class="banner red">' + esc(e.message) + "</div>";
    } finally { zone.classList.remove("busy"); $("#construction-file").value = ""; }
  }

  const zone = $("#dropzone");
  $("#construction-file").addEventListener("change", (e) => upload(e.target.files[0]));
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#construction-file").click(); } });

  // ---------------------------------------------------------------- re-validate
  async function revalidate() {
    const r = await run(() => api("POST", "/api/construction/revalidate/" + runId));
    if (r) {
      const ready = r.readiness.state === "READY_FOR_LOAD";
      App.toast("Re-validated: quality " + r.validation.quality_score + "%, gate " + (ready ? "READY FOR LOAD" : "still BLOCKED") +
        (r.signoff_revoked ? " (earlier sign-off revoked)" : ""), !ready);
      if (App.reloadGovernance) App.reloadGovernance();
    }
    await load();
  }
  $("#revalidate").addEventListener("click", revalidate);
})();
