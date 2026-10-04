(function () {
  "use strict";
  const { $, esc, api, run, badge, statusBadge, confBadge, pct } = App;
  const tableId = App.pageData("tableId");
  let detail = null, filter = null;

  // ---------------------------------------------------------------- mapping sets
  async function loadSets(selectId) {
    const sets = (await run(() => api("GET", "/api/mapping-sets?source_table_id=" + tableId))) || [];
    const sel = $("#mapping-select");
    sel.innerHTML = sets.map((s) => '<option value="' + esc(s.mapping_set_id) + '">#' + esc(s.mapping_set_id) + " v" + esc(s.version) +
      " - " + esc(s.status) + " (" + esc(s.rule_count) + " rules)</option>").join("") || '<option value="">no mapping sets</option>';
    if (selectId) sel.value = String(selectId);
    if (sel.value) await loadDetail(Number(sel.value)); else { detail = null; render(); }
  }

  async function loadDetail(id) {
    detail = await run(() => api("GET", "/api/mapping/" + id));
    filter = null; render();
  }

  // ---------------------------------------------------------------- rendering
  function render() {
    const draft = detail && detail.mapping.status === "DRAFT";
    $("#mapping-status").innerHTML = detail ? statusBadge(detail.mapping.status) +
      (detail.mapping.approved_by ? ' <span class="muted">by ' + esc(detail.mapping.approved_by) + "</span>" : "") : "";
    $("#submit-mapping").disabled = !(detail && draft);
    $("#approve-mapping").disabled = !(detail && detail.mapping.status === "SUBMITTED");
    $("#run-pipeline").disabled = !(detail && detail.mapping.status !== "REJECTED");
    $("#add-rule-toggle").disabled = !draft;
    renderLegacy(); renderRules();
  }

  function renderLegacy() {
    if (!detail) { $("#legacy").innerHTML = '<p class="muted">No mapping set yet.</p>'; return; }
    $("#legacy").innerHTML = detail.source_columns.map((c) =>
      '<div class="legacy-col' + (filter === c.column_name ? " active" : "") + '" data-col="' + esc(c.column_name) + '" tabindex="0">' +
      '<span class="name">' + esc(c.column_name) + "</span> " + (c.mapped ? badge("green", "mapped") : badge("gray", "unmapped")) +
      "<small>" + esc(c.data_type) + " | nulls " + esc(pct(c.null_ratio, 0)) + " | " + esc(c.sample_values.join(", ")) + "</small></div>").join("");
    document.querySelectorAll(".legacy-col").forEach((el) => {
      const toggle = () => { filter = filter === el.dataset.col ? null : el.dataset.col; renderLegacy(); renderRules(); };
      el.addEventListener("click", toggle);
      el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
  }

  function ruleStatus(r) {
    if (r.needs_review) return badge("amber", "Needs review");
    if (r.reviewed_by) return badge("green", "Reviewed by " + r.reviewed_by);
    if (!r.ai_suggested) return badge("gray", "Manual");
    return badge("green", "Validated");
  }

  function renderRules() {
    const box = $("#rules");
    if (!detail) { box.innerHTML = '<p class="muted">Select or generate a mapping set.</p>'; $("#unmapped").innerHTML = ""; return; }
    const rules = detail.rules.filter((r) => !filter || r.source_column === filter);
    const editable = detail.mapping.status === "DRAFT";
    box.innerHTML = rules.map((r) =>
      '<div class="rule" data-rule="' + esc(r.rule_id) + '"><div class="rule-head">' +
      "<div>" + (r.source_column ? "<strong>" + esc(r.source_column) + "</strong>" : '<span class="muted">(constant)</span>') + "</div>" +
      '<div class="arrow">&rarr;</div>' +
      "<div><strong>" + esc(r.target_table) + "." + esc(r.target_column) + "</strong>" + (r.target_mandatory ? ' <span title="mandatory">*</span>' : "") +
      '<div class="muted">' + esc(r.target_data_type) + (r.target_length ? "(" + esc(r.target_length) + ")" : "") + "</div></div>" +
      "<div>" + confBadge(r.confidence) + " " + (r.ai_suggested ? badge("purple", "AI-suggested") : "") + " " + ruleStatus(r) +
      (editable ? ' <button class="btn small edit">Edit</button>' : "") + "</div></div>" +
      '<div class="rule-meta"><span class="badge gray">' + esc(r.rule_type) + "</span>" +
      (r.transformation_logic ? ' <code>' + esc(r.transformation_logic) + "</code>" : "") +
      (r.rule_type === "VALUE_MAPPING" ? " " + esc(r.lookup_count) + " lookup values" : "") +
      (r.reasoning ? "<div>" + esc(r.reasoning) + "</div>" : "") +
      (r.review_reasons.length ? "<div>" + r.review_reasons.map((x) => badge("amber", x)).join(" ") + "</div>" : "") +
      "</div><div class=\"rule-edit hidden\"></div></div>").join("") || '<p class="muted">No rules' + (filter ? " for this column" : "") + ".</p>";

    box.querySelectorAll(".rule").forEach((el) => {
      const btn = el.querySelector(".edit");
      if (btn) btn.addEventListener("click", () => toggleEditor(el, detail.rules.find((x) => x.rule_id === Number(el.dataset.rule))));
    });
    $("#unmapped").innerHTML = detail.unmapped_mandatory_targets.length
      ? '<h2>Mandatory SAP fields without a rule</h2>' + detail.unmapped_mandatory_targets.map((t) => badge("red", t)).join(" ") : "";
  }

  function toggleEditor(el, rule) {
    const box = el.querySelector(".rule-edit");
    if (!box.classList.toggle("hidden")) {
      const types = ["DIRECT_COPY", "VALUE_MAPPING", "SQL_EXPRESSION", "STATIC_VALUE"];
      box.innerHTML = '<div class="grid form">' +
        '<label>Rule type<select name="rule_type">' + types.map((t) => "<option" + (t === rule.rule_type ? " selected" : "") + ">" + t + "</option>").join("") + "</select></label>" +
        '<label class="wide">Expression (SQL guardrails apply)<textarea name="logic" spellcheck="false">' + esc(rule.transformation_logic || "") + "</textarea></label>" +
        '<label class="wide">Lookups as JSON, e.g. {"USA": "US"} (VALUE_MAPPING)<textarea name="lookups" spellcheck="false" placeholder="{}"></textarea></label>' +
        '<label>Note<input name="note" maxlength="300"></label>' +
        '<label><span><input type="checkbox" name="confirm"' + (rule.needs_review ? "" : " checked") + '> Confirm as reviewed</span></label>' +
        '<div class="wide"><button class="btn primary save">Save rule</button></div></div>';
      box.querySelector(".save").addEventListener("click", () => saveRule(box, rule));
    }
  }

  function saveRule(box, rule) {
    run(async () => {
      const q = (n) => box.querySelector('[name="' + n + '"]');
      const body = { rule_type: q("rule_type").value, confirm: q("confirm").checked };
      const logic = q("logic").value.trim();
      body.transformation_logic = body.rule_type === "SQL_EXPRESSION" || body.rule_type === "STATIC_VALUE" ? logic : "";
      if (q("note").value.trim()) body.note = q("note").value.trim();
      const lk = q("lookups").value.trim();
      if (lk) { try { body.lookups = JSON.parse(lk); } catch (e) { throw new Error("Lookups must be valid JSON"); } }
      await api("PUT", "/api/mapping/rules/" + rule.rule_id, body);
      App.toast("Rule updated");
      await loadDetail(detail.mapping.mapping_set_id);
    });
  }

  // ---------------------------------------------------------------- actions
  $("#mapping-select").addEventListener("change", (e) => { if (e.target.value) loadDetail(Number(e.target.value)); });

  $("#generate").addEventListener("click", () => run(async () => {
    $("#generate").disabled = true; App.toast("Generating suggestions - one LLM call per column...");
    try {
      const r = await api("POST", "/api/mapping/generate/" + tableId);
      const saved = r.report.proposals.filter((p) => p.outcome === "SAVED").length;
      App.toast("Draft mapping #" + r.mapping_set_id + ": " + saved + " rules proposed");
      await loadSets(r.mapping_set_id);
    } finally { $("#generate").disabled = false; }
  }));

  $("#add-rule-toggle").addEventListener("click", () => $("#add-rule").classList.toggle("hidden"));
  $("#add-rule-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const f = new FormData(ev.target), body = {};
      f.forEach((v, k) => { if (String(v).trim()) body[k] = String(v).trim(); });
      await api("POST", "/api/mapping/" + detail.mapping.mapping_set_id + "/rules", body);
      App.toast("Rule added"); ev.target.reset(); await loadDetail(detail.mapping.mapping_set_id);
    });
  });

  const govern = (action, msg) => run(async () => {
    await api("POST", "/api/governance/" + action + "/" + detail.mapping.mapping_set_id);
    App.toast(msg); await loadSets(detail.mapping.mapping_set_id);
  });
  $("#submit-mapping").addEventListener("click", () => govern("submit-mapping", "Submitted for approval"));
  $("#approve-mapping").addEventListener("click", () => govern("approve-mapping", "Mapping set approved"));

  $("#run-pipeline").addEventListener("click", () => run(async () => {
    $("#run-pipeline").disabled = true; App.toast("Running transform, deduplication and validation...");
    try {
      const r = await api("POST", "/api/pipeline/run/" + detail.mapping.mapping_set_id, {});
      window.location.href = "/ui/governance/" + r.run_id;
    } finally { $("#run-pipeline").disabled = false; }
  }));

  loadSets();
})();
