(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, stat } = App;
  const gate = (g) => (g ? badge(g === "READY_FOR_LOAD" ? "green" : g === "BLOCKED" ? "red" : "amber", g) : "<span class='muted'>no run validated</span>");
  const link = (url, text, title) => "<a href='" + esc(url) + "' data-title='" + esc(title) + "'>" + esc(text) + "</a>";
  const qscore = (x) => (x === null || x === undefined ? "-" : badge(x >= 90 ? "green" : x >= 70 ? "amber" : "red", Number(x).toFixed(1)));

  function render(d) {
    $("#when").textContent = "As of " + d.generated_at.replace("T", " ").slice(0, 19) + " UTC";
    $("#summary").innerHTML = [
      stat("Tables", num(d.tables.registered) + " (" + num(d.tables.landed) + " landed)"),
      stat("Mapping sets", Object.entries(d.mapping_sets).map(([k, v]) => v + " " + k.toLowerCase()).join(", ") || "none"),
      stat("Runs", Object.entries(d.runs).map(([k, v]) => v + " " + k.toLowerCase()).join(", ") || "none"), stat("Waves", num(d.waves.length)),
    ].join("");
    $("#attention").innerHTML = d.attention.length ? "<ul>" + d.attention.map((a) => "<li>" + esc(a) + "</li>").join("") + "</ul>" : "<p class='muted'>Nothing needs attention.</p>";
    $("#waves").innerHTML = table(["Wave", "Runs", "Latest run", "Gate", "Quality", "Valid records", "Open critical", "Quality trend"],
      d.waves.map((w) => [esc(w.wave), num(w.runs), link("/ui/governance/" + w.latest_run, "run " + w.latest_run, "Governance - run " + w.latest_run), gate(w.gate), qscore(w.quality_score),
        num(w.valid_records), w.open_critical === null ? "-" : num(w.open_critical), esc(w.trend.map((t) => (t === null ? "-" : t)).join(" > "))]));
    $("#quality").innerHTML = table(["Table", "Score", "Change", "High findings", "Assessed"],
      d.data_quality.map((t) => [link("/ui/quality/" + t.table_id, t.table_name, "Quality - " + t.table_name), qscore(t.score), t.change === null ? "-" : (t.change > 0 ? "+" : "") + t.change,
        num(t.high_findings), esc(t.assessed_at.replace("T", " ").slice(0, 19))]));
  }

  function change(list, label) {
    return list.length ? "<p><strong>" + esc(label) + "</strong></p>" + table(["Defect group", "Before", "After", "Change"],
      list.map((c) => [esc(c.signature), num(c.before), num(c.after), (c.change > 0 ? "+" : "") + num(c.change)])) : "";
  }
  function renderComparison(c) {
    $("#comparison").innerHTML = "<p>" + esc(c.verdict) + "</p><div class='cards'>" + [
      stat("Quality", c.base.quality_score + " > " + c.other.quality_score), stat("Valid records", num(c.base.valid_records) + " > " + num(c.other.valid_records)),
      stat("Defective", num(c.base.defective_records) + " > " + num(c.other.defective_records)), stat("Unchanged groups", num(c.unchanged))].join("") + "</div>" +
      change(c.resolved, "Resolved") + change(c.new, "New") + change(c.improved, "Improved") + change(c.worsened, "Worse");
  }

  const load = () => run(async () => render(await api("GET", "/api/dashboard")));
  $("#reload").addEventListener("click", load);
  $("#compare").addEventListener("click", () => run(async () => {
    const a = Number($("#cmp-base").value), b = Number($("#cmp-other").value);
    if (!a || !b) { App.toast("Enter both run numbers.", true); return; }
    renderComparison(await api("GET", "/api/cycles/compare?base=" + a + "&other=" + b));
  }));
  load();
})();
