(function () {
  "use strict";
  const { $, esc, api, run, badge, num } = App;
  const root = $("#assistant");
  if (!root) return;
  const isRun = root.dataset.scope === "run";
  const scope = () => (isRun ? { run_id: App.pageData("runId") } : (App.studioScope ? App.studioScope() : {}));
  const EXAMPLES = isRun
    ? ["How did this run go?", "Why were records rejected?", "Which rejected records have no country?", "What changed since the last run?", "Which records were left out, and why?"]
    : ["What should I do next?", "Which rules still need a review?", "How many rules did the AI suggest?", "Tell me about the country column"];

  root.insertAdjacentHTML("beforeend", '<p class="muted">Try: ' + EXAMPLES.map((q) => '<button class="btn small example" type="button">' + esc(q) + "</button>").join(" ") + "</p>");
  root.querySelectorAll(".example").forEach((b) => b.addEventListener("click", () => { $("#ask-question").value = b.textContent; ask(); }));
  $("#ask-go").addEventListener("click", ask);
  $("#ask-question").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ask(); } });

  function ask() {
    const question = $("#ask-question").value.trim();
    if (question.length < 3) { App.toast("Type a question first", true); return; }
    run(async () => {
      $("#ask-go").disabled = true;
      $("#ask-result").innerHTML = '<p class="muted"><span class="spinner"></span> Looking it up in the data...</p>';
      try { render(await api("POST", "/api/assistant/ask", Object.assign({ question }, scope()))); }
      catch (e) { $("#ask-result").innerHTML = ""; throw e; }
      finally { $("#ask-go").disabled = false; }
    });
  }

  function render(a) {
    const flag = a.source === "model" ? badge("green", "every figure checked against the data")
      : badge("amber", "plain statement of the facts (no AI wording)");
    $("#ask-result").innerHTML = '<div class="card answer"><p><strong>' + esc(a.answer) + "</strong></p><p>" + flag + "</p>" +
      (a.notes.length ? "<ul class=\"findings\">" + a.notes.map((n) => "<li>" + esc(n) + "</li>").join("") + "</ul>" : "") +
      '<details><summary class="muted">What I looked at</summary><ul class="findings">' + a.tool_runs.map((t) => "<li>" + badge(t.ok ? "green" : "red", t.tool) + " " + esc(t.summary) + "</li>").join("") + "</ul></details></div>" +
      a.tables.map((t) => "<h2>" + esc(t.title) + (t.total > t.rows.length ? ' <span class="muted">(showing ' + esc(t.rows.length) + " of " + esc(num(t.total)) + ")</span>" : "") + "</h2>" +
        (t.rows.length ? App.table(t.columns, t.rows.map((r) => r.map((c) => esc(c)))) : '<p class="muted">Nothing matches.</p>')).join("") +
      '<div class="fixes"></div>';
    App.renderFixes($("#ask-result .fixes"), a.fixes, async (r) => { if (App.studioRefresh) await App.studioRefresh(r.mapping_set_id); });
  }
})();
