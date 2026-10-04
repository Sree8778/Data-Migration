(function () {
  "use strict";
  const { $, esc, api, run, table, num, pct, badge, band, stat, bar } = App;
  const tableId = App.pageData("tableId");

  const FLAG_TEXT = {
    HIGH_NULLS: "many nulls", LEADING_ZERO_RISK: "leading zeros at risk", CONSTANT: "constant value",
    NEAR_UNIQUE_KEY: "candidate key", EMAIL_PATTERN_VIOLATIONS: "invalid e-mails",
    COUNTRY_ISO_PATTERN_VIOLATIONS: "non-ISO country values", POSTAL_PATTERN_VIOLATIONS: "odd postal codes",
  };

  function render(p) {
    $("#profile-meta").textContent = "Profiled " + p.profiled_at.replace("T", " ").slice(0, 19) + " UTC";
    $("#summary").innerHTML = [
      stat("Rows", num(p.total_rows)), stat("Columns", num(p.column_count)),
      stat("Average health", p.average_health_score.toFixed(1)),
      stat("Duplicate rows", num(p.full_row_duplicate_rows) + " (" + pct(p.full_row_duplicate_ratio) + ")"),
      stat("Inferred keys", num(p.inferred_keys.length)),
    ].join("");

    $("#columns").innerHTML = table(
      ["Column", "Type", "Null ratio", "Distinct", "Length", "Top values", "Sample (masked if PII)", "Health", "Flags"],
      p.columns.map((c) => [
        "<strong>" + esc(c.column_name) + "</strong>", esc(c.detected_type),
        bar(c.null_ratio) + " " + esc(pct(c.null_ratio)), esc(pct(c.distinct_ratio)) + " <span class='muted'>(" + num(c.distinct_count) + ")</span>",
        esc(c.min_length === null ? "-" : c.min_length + "-" + c.max_length),
        "<span class='clip mono'>" + esc(c.top_values.slice(0, 3).map((t) => t.value + " x" + t.count).join(", ")) + "</span>",
        "<span class='clip mono'>" + esc(c.sample_values.slice(0, 3).join(", ")) + "</span>" + (c.pii_masked ? " " + badge("purple", "masked") : ""),
        badge(band(c.health_score / 100), c.health_score.toFixed(0)),
        c.flags.map((f) => badge(f === "NEAR_UNIQUE_KEY" ? "gray" : "amber", FLAG_TEXT[f] || f)).join(" "),
      ]));

    const keyRows = p.key_duplicates.map((k) => [
      "<strong>" + esc(k.columns.join(" + ")) + "</strong>", num(k.duplicate_group_count), num(k.duplicate_row_count),
      "<span class='mono'>" + esc(k.sample_duplicate_keys.slice(0, 3).map((s) => s.join("|")).join(", ")) + "</span>",
      k.duplicate_row_count === 0 ? badge("green", "unique") : badge("amber", "duplicates"),
    ]);
    $("#dupes").innerHTML = "<p>Full-row duplicates: <strong>" + num(p.full_row_duplicate_rows) + "</strong>. Candidate keys are columns that are fully populated and at least 95% unique.</p>" +
      table(["Candidate key", "Duplicate groups", "Surplus rows", "Sample keys", "Result"], keyRows);
  }

  async function load() {
    try { render(await api("GET", "/api/profile/" + tableId)); }
    catch (e) {
      $("#columns").innerHTML = '<p class="muted">' + esc(e.status === 404 ? "Not profiled yet - run profiling." : e.message) + "</p>";
      $("#dupes").innerHTML = ""; $("#summary").innerHTML = "";
    }
  }

  $("#run-profile").addEventListener("click", () => run(async () => {
    $("#run-profile").disabled = true;
    try { render(await api("POST", "/api/profile/" + tableId)); App.toast("Profiling complete"); }
    finally { $("#run-profile").disabled = false; }
  }));
  load();
})();
