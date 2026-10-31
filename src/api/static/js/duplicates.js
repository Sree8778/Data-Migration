(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, stat } = App;
  const runId = App.pageData("runId");
  const base = "/api/duplicates/" + runId;
  const decisionBadge = (d, by) => (d ? badge(d === "MERGE" ? "amber" : "green", (d === "MERGE" ? "same entity" : "different") + (by ? " (" + by + ")" : "")) : badge("gray", "undecided"));
  const rec = (pk, name, addr) => "<strong>" + esc(pk) + "</strong><br>" + esc(name || "") + (addr ? "<br><span class='muted'>" + esc(addr) + "</span>" : "");

  function buttons(a, b) {
    const attrs = "data-a='" + esc(a) + "' data-b='" + esc(b) + "'";
    return "<button type='button' class='btn small' data-decide='MERGE' " + attrs + ">Same entity</button> " +
      "<button type='button' class='btn small' data-decide='DISTINCT' " + attrs + ">Different</button> " +
      "<button type='button' class='btn small' data-decide='CLEAR' " + attrs + ">Clear</button>";
  }

  async function load() {
    const undecided = $("#undecided-only").checked;
    const [p, m, d] = await Promise.all([
      api("GET", base + "/possible?limit=200" + (undecided ? "&undecided_only=true" : "")),
      api("GET", base + "/merges?strength=NAME_ONLY&limit=200"), api("GET", base + "/decisions")]);
    $("#summary").innerHTML = [stat("Possible duplicates", num(p.total)), stat("Still to decide", num(p.undecided)), stat("Merged on the name alone", num(m.total)), stat("Decisions kept", num(d.total))].join("");
    $("#possible-count").textContent = "(" + num(p.items.length) + " shown)";
    $("#possible").innerHTML = table(["Similarity", "Record A", "Record B", "Decision", ""], p.items.map((x) => [
      esc(x.similarity) + "%", rec(x.pk_a, x.name_a, x.address_a), rec(x.pk_b, x.name_b, x.address_b), decisionBadge(x.decision, x.decided_by), buttons(x.pk_a, x.pk_b)]));
    $("#merges-count").textContent = "(" + num(m.items.length) + " shown)";
    $("#merges").innerHTML = table(["Kept record", "Merged away (not loaded)", "Evidence", "Decision", ""], m.items.map((x) => [
      rec(x.master_pk, x.master_name), rec(x.child_pk, x.child_name), esc(x.match_type) + " " + badge("amber", x.strength), decisionBadge(x.decision, x.decided_by),
      "<button type='button' class='btn small' data-decide='DISTINCT' data-a='" + esc(x.master_pk) + "' data-b='" + esc(x.child_pk) + "'>These are two entities</button> " +
      "<button type='button' class='btn small' data-decide='MERGE' data-a='" + esc(x.master_pk) + "' data-b='" + esc(x.child_pk) + "'>Confirm the merge</button>"]));
    $("#decisions").innerHTML = table(["Pair", "Decision", "By", "When (UTC)", "Reason"], d.decisions.map((x) => [
      esc(x.pk_a + " / " + x.pk_b), decisionBadge(x.decision), esc(x.by), esc(x.at.replace("T", " ").slice(0, 19)), esc(x.reason || "")]));
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-decide]");
    if (!b) return;
    run(async () => {
      await api("POST", base + "/decide", { decisions: [{ pk_a: b.dataset.a, pk_b: b.dataset.b, decision: b.dataset.decide }] });
      App.toast("Recorded. It applies from the next run.");
      await load();
    });
  });
  $("#undecided-only").addEventListener("change", () => run(load));
  run(load);
})();
