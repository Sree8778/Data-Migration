(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, stat, bar } = App;
  const tableId = App.pageData("tableId");
  const sev = (s) => badge(s === "HIGH" ? "red" : s === "MEDIUM" ? "amber" : "gray", s);
  const score = (x) => badge(x >= 90 ? "green" : x >= 70 ? "amber" : "red", Number(x).toFixed(1));

  function render(r) {
    $("#assess-meta").textContent = "Assessment " + r.assessment_no + ", " + r.assessed_at.replace("T", " ").slice(0, 19) + " UTC, by " + r.assessed_by;
    const change = r.change === null || r.change === undefined ? "first assessment" : (r.change > 0 ? "+" : "") + r.change + " since the last";
    $("#summary").innerHTML = [
      stat("Quality score", r.overall_score.toFixed(1)), stat("Change", change), stat("Rows", num(r.rows)), stat("Checks run", num(r.rules_run)),
      stat("Findings", num(r.findings.length)), stat("High", num(r.findings.filter((f) => f.severity === "HIGH").length)),
    ].join("") + Object.entries(r.dimension_scores).map(([d, s]) => stat(d, s.toFixed(1))).join("");
    const notes = r.notes.length ? '<p class="muted">' + r.notes.map(esc).join(" ") + "</p>" : "";
    $("#findings").innerHTML = notes + (r.findings.length ? table(
      ["Severity", "Rule", "Column", "Dimension", "Affected", "Examples", "What to do"],
      r.findings.map((f) => [sev(f.severity), "<strong>" + esc(f.rule_id) + "</strong> " + esc(f.title), esc(f.column) + " <span class='muted'>(" + esc(f.role) + ")</span>", esc(f.dimension),
        num(f.violations) + " of " + num(f.checked) + " " + bar(f.rate), "<span class='clip mono'>" + esc(f.samples.join(" | ")) + "</span>", esc(f.hint)])
    ) : "<p class='muted'>No finding: every rule passed.</p>");
    $("#columns").innerHTML = table(["Column", "Role", "Personal data", "Filled", "Score", "Findings"],
      r.columns.map((c) => ["<strong>" + esc(c.column) + "</strong>", esc(c.role), c.pii === "INTERNAL" ? "<span class='muted'>no</span>" : badge("purple", c.pii), num(c.filled), score(c.score), num(c.findings)]));
  }

  async function history() {
    const h = await api("GET", "/api/quality/" + tableId + "/history");
    return showHistory(h);
  }

  function showHistory(h) {
    $("#history").innerHTML = table(["#", "When (UTC)", "By", "Score", "Findings", "High", "Rows"],
      h.slice().reverse().map((x) => [num(x.assessment_no), esc(x.assessed_at.replace("T", " ").slice(0, 19)), esc(x.assessed_by), score(x.overall_score), num(x.findings), num(x.high_findings), num(x.rows)]));
  }

  async function load() {
    try {
      const h = await api("GET", "/api/quality/" + tableId + "/history");           // asked first: an empty list is an answer, a 404 for "not assessed yet" is a console error
      if (!h.length) { $("#findings").innerHTML = "<p class='muted'>This table has not been assessed yet. Press the button.</p>"; $("#summary").innerHTML = ""; return; }
      render(await api("GET", "/api/quality/" + tableId)); showHistory(h);
    } catch (e) { App.toast(e.message, true); }
  }

  $("#assess").addEventListener("click", () => run(async () => {
    $("#assess").disabled = true; App.toast("Assessing...");
    try { render(await api("POST", "/api/quality/assess/" + tableId)); await history(); App.toast("Assessment done."); }
    finally { $("#assess").disabled = false; }
  }));
  load();
})();
