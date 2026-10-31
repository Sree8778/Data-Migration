(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, stat } = App;

  $("#export").addEventListener("click", () => {
    const id = Number($("#x-id").value);
    if (!id) { App.toast("Enter a mapping set number.", true); return; }
    const name = $("#x-name").value.trim();
    App.download("/api/packs/export/" + id + (name ? "?name=" + encodeURIComponent(name) : ""), "content-pack-mapping-" + id + ".json");
  });

  function show(r) {
    $("#result").innerHTML = "<p>" + (r.dry_run ? badge("amber", "dry run: nothing written") : badge("green", "draft mapping set " + r.mapping_set_id + " created")) + " " + esc(r.pack_name) +
      " <span class='muted'>(checksum " + esc(r.pack_sha256.slice(0, 12)) + ")</span></p><div class='cards'>" +
      [stat("Rules imported", num(r.rules_imported)), stat("Rules skipped", num(r.rules_skipped.length)), stat("Lookup entries", num(r.lookup_entries)),
       stat("Exclusions", num(r.exclusions_imported) + " (" + num(r.exclusions_skipped.length) + " skipped)")].join("") + "</div>" +
      (r.rules_skipped.length ? "<h3>Skipped rules</h3>" + table(["Rule", "Why"], r.rules_skipped.map((x) => [esc(x.what), esc(x.reason)])) : "") +
      (r.exclusions_skipped.length ? "<h3>Skipped exclusions</h3>" + table(["Rule", "Why"], r.exclusions_skipped.map((x) => [esc(x.what), esc(x.reason)])) : "") +
      "<h3>Next</h3><ul>" + r.next_steps.map((s) => "<li>" + esc(s) + "</li>").join("") + "</ul>";
  }

  $("#import").addEventListener("click", () => run(async () => {
    const tableId = Number($("#i-table").value), file = $("#i-file").files[0];
    if (!tableId || !file) { App.toast("Choose a table and a pack file.", true); return; }
    let pack;
    try { pack = JSON.parse(await file.text()); } catch (e) { App.toast("That file is not JSON.", true); return; }
    show(await api("POST", "/api/packs/import/" + tableId, { pack, dry_run: $("#i-dry").checked }));
  }));
})();
