(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, stat } = App;
  const KIND = { PASS: "green", WARN: "amber", FAIL: "red", INFO: "gray" };

  function render(r) {
    $("#summary").innerHTML = ["PASS", "WARN", "FAIL", "INFO"].map((k) => stat(k, num(r.summary[k] || 0))).join("") + stat("Access control", r.auth_mode);
    $("#controls").innerHTML = table(["", "Control", "Standards", "Evidence", "What to do"],
      r.controls.map((c) => [badge(KIND[c.status], c.status), "<strong>" + esc(c.id) + "</strong> " + esc(c.title) + "<br><span class='muted'>" + esc(c.area) + "</span>",
        "<span class='muted'>" + c.standards.map(esc).join("<br>") + "</span>", esc(c.evidence), esc(c.action || "")]));
  }

  async function audit() {
    try {
      const a = await api("GET", "/api/compliance/audit-log?limit=50");
      $("#audit").innerHTML = table(["#", "Time (UTC)", "User", "Action", "Outcome", "Status"],
        a.entries.map((e) => [num(e.seq), esc(e.ts.replace("T", " ").slice(0, 19)), esc(e.user || "-"), esc(e.method + " " + e.path + (e.phase === "intent" ? " (intent)" : "")),
          badge(e.outcome === "ok" ? "green" : e.outcome === "error" ? "red" : e.outcome ? "amber" : "gray", e.outcome || e.phase), esc(e.status || "")]));
    } catch (e) { $("#audit").innerHTML = "<p class='muted'>" + esc(e.message) + "</p>"; }
  }

  async function load() {
    try { render(await api("GET", "/api/compliance/report")); }
    catch (e) { $("#controls").innerHTML = "<p class='muted'>" + esc(e.status === 403 ? "You need the auditor role to see this. " : "") + esc(e.message) + "</p>"; return; }
    await audit();
  }
  $("#reload").addEventListener("click", load);
  $("#verify").addEventListener("click", () => run(async () => {
    const v = await api("GET", "/api/compliance/audit-log/verify");
    $("#verify-result").textContent = v.ok ? "Intact: " + num(v.entries) + " entries" + (v.keyed ? ", keyed chain" : "") + ", head #" + v.head.seq + " " + String(v.head.hash).slice(0, 12) + "..."
      : "BROKEN at entry " + v.first_bad_seq + ": " + v.reason;
    App.toast(v.ok ? "The audit trail is intact." : "The audit trail is NOT intact.", !v.ok);
  }));
  load();
})();
