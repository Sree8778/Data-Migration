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
    $("#tables").innerHTML = table(["Table", "System", "Columns", "Rows", "Last profiled", "Source", ""],
      tables.map((t) => [
        "<strong>" + esc(t.table_name) + "</strong> <span class='muted'>#" + esc(t.table_id) + "</span>", esc(t.system_name),
        num(t.column_count), num(t.row_count_estimate), esc(t.last_profiled_at ? t.last_profiled_at.replace("T", " ").slice(0, 19) : "never"),
        t.has_source_file ? badge("green", t.source_format || "uploaded") : badge("gray", "none"),
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

  // ---------------------------------------------------------------- how the file is read: options + preview
  function readOptions() {
    const o = { header: $("#opt-header").checked };
    const set = (key, value) => { if (value !== "" && value !== null && !Number.isNaN(value)) o[key] = value; };
    set("format", $("#opt-format").value); set("delimiter", $("#opt-delimiter").value);
    set("encoding", $("#opt-encoding").value.trim()); set("sheet", $("#opt-sheet").value); set("member", $("#opt-member").value);
    if (Number($("#opt-header-row").value) > 1) o.header_row = Number($("#opt-header-row").value);
    if (Number($("#opt-skip").value) > 0) o.skip_rows = Number($("#opt-skip").value);
    const tokens = $("#opt-nulls").value.split(",").map((t) => t.trim()).filter(Boolean);
    if (tokens.length) o.null_tokens = tokens;
    const layout = $("#opt-fixed").value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => l.split(","));
    if (layout.length) o.columns = layout.map(([name, start, width]) => ({ name: (name || "").trim(), start: Number(start), width: Number(width) }));
    return o;
  }

  function fillOptions(p) {   // the resolved options come back with the detection results filled in
    const o = p.options;
    $("#opt-format").value = o.format || ""; $("#opt-delimiter").value = o.delimiter || ""; $("#opt-encoding").value = o.encoding || "";
    const sheet = $("#opt-sheet");
    sheet.innerHTML = '<option value="">first visible sheet</option>' + p.sheets.map((s) => "<option>" + esc(s) + "</option>").join("");
    sheet.value = o.sheet || "";
    const member = $("#opt-member");
    member.innerHTML = '<option value="">(only one file)</option>' + p.members.map((s) => "<option>" + esc(s) + "</option>").join("");
    member.value = o.member || "";
  }

  async function preview() {
    const file = $("#reg-file").files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file); form.append("options", JSON.stringify(readOptions()));
    $("#read-options").classList.remove("hidden");
    $("#preview-table").innerHTML = '<p class="muted">Reading...</p>';
    try {
      const p = await api("POST", "/api/sources/preview", form, true);
      fillOptions(p);
      $("#detected").innerHTML = badge("gray", "format: " + p.format) + (p.encoding ? badge("gray", "encoding: " + p.encoding + " (" + p.encoding_basis + ")") : "") +
        (p.delimiter ? badge("gray", "delimiter: " + (p.delimiter === "\t" ? "tab" : p.delimiter)) : "") + (p.truncated ? badge("amber", "preview of the first 8 MB") : "");
      $("#preview-warnings").innerHTML = p.warnings.map((w) => '<div class="banner amber">' + esc(w) + "</div>").join("");
      $("#preview-table").innerHTML = table(p.columns, p.rows.map((r) => r.map((c) => (c === null ? '<span class="muted">null</span>' : "<code>" + esc(c) + "</code>"))));
    } catch (e) {
      $("#preview-warnings").innerHTML = "";
      $("#preview-table").innerHTML = '<div class="banner red">' + esc(e.message) + "</div>";
    }
  }
  $("#reg-file").addEventListener("change", preview);
  $("#refresh-preview").addEventListener("click", preview);

  $("#register-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const form = new FormData(ev.target);
      form.append("options", JSON.stringify(readOptions()));
      if ($("#opt-expected").value !== "") form.append("expected_rows", $("#opt-expected").value);
      const r = await api("POST", "/api/sources/register", form, true);
      App.toast("Registered table #" + r.table_id + " (" + r.columns_registered + " columns, " + App.num(r.rows_landed) + " rows, " + r.format + ")" +
        (r.warnings.length ? " - " + r.warnings.length + " warning(s)" : ""));
      ev.target.reset(); $("#read-options").classList.add("hidden"); loadTables();
    });
  });

  loadHealth(); loadTables(); loadRuns();
})();
