(function () {
  "use strict";
  const { $, esc, api, run, badge, statusBadge, confBadge, pct } = App;
  const tableId = App.pageData("tableId");
  let detail = null, filter = null, sets = [], preflight = null, exclusions = null, target = null, targets = [];

  // ---------------------------------------------------------------- mapping sets
  async function loadSets(selectId) {
    const archivedToo = $("#show-archived").checked ? "&include_archived=true" : "";
    sets = (await run(() => api("GET", "/api/mapping-sets?source_table_id=" + tableId + archivedToo))) || [];
    const sel = $("#mapping-select");
    sel.innerHTML = sets.map((s) => '<option value="' + esc(s.mapping_set_id) + '">#' + esc(s.mapping_set_id) + " v" + esc(s.version) +
      " - " + esc(s.status) + (s.archived ? " [archived]" : "") + " (" + esc(s.rule_count) + " rules)</option>").join("") ||
      '<option value="">no mapping sets</option>';
    if (selectId) sel.value = String(selectId);
    if (sel.value) await loadDetail(Number(sel.value)); else { detail = null; render(); }
  }

  async function loadDetail(id) {
    detail = await run(() => api("GET", "/api/mapping/" + id));
    preflight = await api("GET", "/api/mapping/" + id + "/preflight").catch(() => null);   // null until a check has been run
    target = await api("GET", "/api/mapping/" + id + "/target").catch(() => null);          // where the result is meant to go (null connection = SAP)
    filter = null; render();
    loadExclusions();
  }

  // ---------------------------------------------------------------- rows to leave out
  async function loadExclusions() {
    if (!detail) { exclusions = null; renderExclusions([]); return; }
    const id = detail.mapping.mapping_set_id;
    exclusions = await api("GET", "/api/mapping/" + id + "/exclusions").catch(() => null);
    const suggestions = exclusions && exclusions.editable ? await api("GET", "/api/mapping/" + id + "/exclusions/suggestions").catch(() => []) : [];
    renderExclusions(suggestions || []);
    renderPreflight(); renderRules();   // the stored check is only 'current' once the exclusion rules are known
  }

  const OPS = { equals: "equals", in: "is one of", is_blank: "is blank", starts_with: "starts with", contains: "contains", regex: "matches" };
  const ruleText = (r) => esc(r.column) + " " + esc(OPS[r.operator] || r.operator) + (r.values.length ? " <code>" + esc(r.values.join(", ")) + "</code>" : "");

  function renderExclusions(suggestions) {
    const box = $("#excl-rules"), form = $("#excl-form");
    if (!exclusions) { box.innerHTML = ""; $("#excl-suggest").innerHTML = ""; $("#excl-summary").textContent = ""; form.classList.add("hidden"); return; }
    const s = exclusions.summary, n = exclusions.rules.length;
    const hint = suggestions.length ? " (the data suggests " + suggestions.length + ")" : "";
    $("#excl-summary").textContent = n ? "- " + n + " rule(s)" + (s ? ", " + App.num(s.excluded_rows) + " of " + App.num(s.total_rows) + " rows left out" : "") + hint : "- none" + hint;
    $("#exclusions").open = n > 0 || suggestions.length > 0 || $("#exclusions").open;
    const counts = new Map((s ? s.rules : []).map((c) => [c.rule_id, c]));
    box.innerHTML = n ? exclusions.rules.map((r) => { const c = counts.get(r.rule_id);
      return '<div class="rule" data-excl="' + esc(r.rule_id) + '"><div class="rule-head"><div>' + ruleText(r) + "</div><div></div><div>" +
        (c ? badge(c.excluded ? "amber" : "gray", App.num(c.excluded) + " row(s)") : "") +
        (exclusions.editable ? ' <button class="btn small excl-delete">Delete</button>' : "") + "</div></div>" +
        '<div class="rule-meta">' + esc(r.reason) + ' <span class="muted">(by ' + esc(r.created_by) + (c && c.examples.length ? "; e.g. " + esc(c.examples.join(", ")) : "") + ")</span></div></div>"; }).join("")
      : '<p class="muted">No rows are left out: everything in the file is migrated.</p>';
    box.querySelectorAll(".excl-delete").forEach((btn) => btn.addEventListener("click", () => run(async () => {
      const id = Number(btn.closest("[data-excl]").dataset.excl);
      if (!window.confirm("Stop leaving these rows out? The rule is removed and the change is recorded in the audit log.")) return;
      await api("DELETE", "/api/mapping/" + detail.mapping.mapping_set_id + "/exclusions/" + id);
      App.toast("Rule removed"); await loadExclusions(); renderPreflight(); renderRules();
    })));
    form.classList.toggle("hidden", !exclusions.editable);
    const chosen = $("#excl-column").value;   // reloading the rules must not throw away what the person has already picked
    $("#excl-column").innerHTML = detail.source_columns.filter((c) => !c.missing_from_file)
      .map((c) => "<option>" + esc(c.column_name) + "</option>").join("");
    if (chosen) $("#excl-column").value = chosen;
    $("#excl-suggest").innerHTML = exclusions.editable && suggestions.length
      ? '<p><strong>Suggested by the data</strong> (nothing is applied until you accept):</p>' + suggestions.map((x, i) =>
        '<div class="rule"><div class="rule-head"><div>' + ruleText(x) + "</div><div></div><div>" + badge("amber", App.num(x.matches) + " row(s)") +
        ' <button class="btn small excl-use" data-i="' + i + '">Use this</button></div></div><div class="rule-meta">' + esc(x.reason) +
        (x.examples.length ? ' <span class="muted">e.g. ' + esc(x.examples.join(", ")) + "</span>" : "") + "</div></div>").join("") : "";
    $("#excl-suggest").querySelectorAll(".excl-use").forEach((btn) => btn.addEventListener("click", () => {
      const x = suggestions[Number(btn.dataset.i)];
      $("#excl-column").value = x.column; $("#excl-operator").value = x.operator; $("#excl-value").value = x.values.join(", ");
      $("#excl-reason").value = x.reason; syncOperator(); checkExclusion(); $("#excl-reason").focus();
    }));
    syncOperator();
  }

  const syncOperator = () => { $("#excl-value").disabled = $("#excl-operator").value === "is_blank"; };
  function exclusionBody() {
    const op = $("#excl-operator").value, raw = $("#excl-value").value;
    const values = op === "is_blank" ? [] : op === "in" ? raw.split(",").map((v) => v.trim()).filter(Boolean) : [raw.trim()];
    return { column: $("#excl-column").value, operator: op, values, reason: $("#excl-reason").value.trim() || "(reason pending)" };
  }
  async function checkExclusion() {
    $("#excl-preview").textContent = "checking...";
    try {
      const c = await api("POST", "/api/mapping/" + detail.mapping.mapping_set_id + "/exclusions/preview", exclusionBody());
      $("#excl-preview").textContent = App.num(c.excluded) + " row(s) match" + (c.examples.length ? ", for example " + c.examples.join(", ") : "");
    } catch (e) { $("#excl-preview").textContent = e.message; }
  }
  $("#excl-operator").addEventListener("change", syncOperator);
  $("#excl-check").addEventListener("click", checkExclusion);
  $("#excl-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const c = await api("POST", "/api/mapping/" + detail.mapping.mapping_set_id + "/exclusions", exclusionBody());
      App.toast(App.num(c.excluded) + " row(s) will be left out"); ev.target.reset(); $("#excl-preview").textContent = "";
      await loadExclusions(); renderPreflight(); renderRules();
    });
  });

  // ---------------------------------------------------------------- the pre-submission check
  // The stored report describes the rules as they were when it ran: if they were edited since, it is shown as out of date and not used per rule.
  function preflightStale() {
    if (!preflight || !detail) return true;
    if ((preflight.exclusion_rules || []).join(",") !== (exclusions ? exclusions.rules.map((r) => r.rule_id) : []).join(",")) return true;
    if (((preflight.destination && preflight.destination.connection) || "") !== ((target && target.connection) || "")) return true;     // the destination changed after the check
    const now = detail.rules.map((r) => r.rule_id + ":" + r.rule_type + ":" + r.lookup_count).sort().join(",");
    return preflight.rules.map((r) => r.rule_id + ":" + r.rule_type + ":" + r.lookup_count).sort().join(",") !== now;
  }
  const ruleCheck = (r) => (preflight && !preflightStale() ? preflight.rules.find((c) => c.rule_id === r.rule_id) : null);

  // what the destination says about this delivery: the cost, in the plain; its blockers and warnings are in the list of findings below, with what to do
  function destinationHtml(p) {
    if (!p.destination) return "";
    const info = p.destination.checks.filter((c) => c.level === "info");
    return '<div class="destination"><h3>Destination: ' + esc(p.destination.label) + "</h3>" +
      (info.length ? "<ul>" + info.map((c) => "<li>" + esc(c.message) + "</li>").join("") + "</ul>" : "") + "</div>";
  }

  async function loadTargets() {
    targets = (await api("GET", "/api/connections?role=target").catch(() => [])) || [];
    $("#target-select").innerHTML = '<option value="">SAP S/4HANA (default)</option>' +
      targets.map((t) => '<option value="' + esc(t.name) + '">' + esc(t.label + " (" + t.name + ")") + "</option>").join("");
    renderTarget();
  }
  function renderTarget() {
    const sel = $("#target-select"), editable = detail && (detail.mapping.status === "DRAFT" || detail.mapping.status === "REJECTED");
    sel.value = target && target.connection && targets.some((t) => t.name === target.connection) ? target.connection : "";
    sel.disabled = !editable;
  }
  $("#target-select").addEventListener("change", () => run(async () => {
    const r = await api("PUT", "/api/mapping/" + detail.mapping.mapping_set_id + "/target", { connection: $("#target-select").value || null });
    target = r;
    App.toast(r.connection ? "Destination: " + r.label + ". Run 'Check rules on sample' to see what it says." : "Destination: SAP S/4HANA (the default)");
    renderPreflight(); renderTarget();
  }));

  function renderPreflight() {
    const box = $("#preflight-panel");
    if (!preflight || !detail || preflight.mapping_set_id !== detail.mapping.mapping_set_id) { box.classList.add("hidden"); return; }
    const p = preflight, stale = preflightStale();
    box.classList.remove("hidden");
    box.innerHTML = "<h2>Pre-submission check</h2><p>" +
      (p.can_submit ? badge("green", "can be submitted") : badge("red", "cannot be submitted: " + p.blockers + " problem(s) to fix")) +
      (p.warnings ? " " + badge("amber", p.warnings + " warning(s)") : "") + (stale ? " " + badge("gray", "out of date: the rules changed after this check") : "") +
      ' <span class="muted">' + (p.sampled
        ? "dry run on " + esc(App.num(p.sample_rows)) + " of " + esc(App.num(p.source_rows)) + " rows (the same rows every time): " + esc(pct(p.valid_rate, 0)) + " pass every rule"
        : "structure only, no sample could be run") + "; checked " + esc(new Date(p.checked_at).toLocaleString()) +
      (p.models.length ? "; model " + esc(p.models.join(", ")) : "") + "</span></p>" + destinationHtml(p) +
      (p.next_steps && p.next_steps.length ? '<div class="next-steps"><h3>What to do next</h3><ol>' + p.next_steps.map((t) => "<li>" + esc(t) + "</li>").join("") + "</ol></div>" : "") +
      (p.findings.length ? '<ul class="findings">' + p.findings.map((f, i) => "<li>" + badge(f.severity === "BLOCKER" ? "red" : "amber", f.severity) + " " + esc(f.message) +
        (f.next_step ? '<div class="what-to-do"><strong>What to do:</strong> ' + esc(f.next_step) + (ACTION_LABEL[f.action] ? ' <button class="btn small finding-action" type="button" data-i="' + i + '">' + esc(ACTION_LABEL[f.action]) + "</button>" : "") + "</div>" : "") +
        "</li>").join("") + "</ul>"
        : '<p class="muted">Nothing to report.</p>');
    box.querySelectorAll(".finding-action").forEach((b) => b.addEventListener("click", () => doFindingAction(p.findings[Number(b.dataset.i)])));
    if (!stale && p.findings.length) {
      box.insertAdjacentHTML("beforeend", '<div class="fixes"></div>');
      api("GET", "/api/mapping/" + p.mapping_set_id + "/fixes?stored=true").then((fixes) => App.renderFixes(box.querySelector(".fixes"), fixes, (r) => App.studioRefresh(r.mapping_set_id))).catch(() => null);
    }
  }

  // The button on a finding does the next step: it opens the rule (and, for a lookup, starts the AI draft), or presses the toolbar button that fixes it.
  const ACTION_LABEL = { draft_lookups: "Open the rule and draft lookups with AI", edit_rule: "Open the rule", review_rules: "Open the first rule to review",
    add_placeholders: "Add placeholders now", generate: "Add a manual rule" };
  function openRule(ruleId, thenDraft) {
    if (filter) { filter = null; renderRules(); }                                  // a column filter could be hiding the rule
    const el = document.querySelector('#rules .rule[data-rule="' + ruleId + '"]');
    if (!el) { App.toast("That rule is not on the screen: pick the mapping set again", true); return; }
    const editor = el.querySelector(".rule-edit");
    if (editor.classList.contains("hidden")) el.querySelector(".edit").click();
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.add("pulse");
    setTimeout(() => el.classList.remove("pulse"), 2500);
    if (thenDraft) {
      const draft = el.querySelector(".draft-lookups");
      if (draft) draft.click(); else App.toast("This rule is not a value mapping yet: change its type to VALUE_MAPPING and save, then press 'Draft lookups with AI'");
    }
  }
  function doFindingAction(f) {
    if (f.action === "draft_lookups" && f.rule_id) return openRule(f.rule_id, true);
    if (f.action === "edit_rule" && f.rule_id) return openRule(f.rule_id, false);
    if (f.action === "review_rules" && f.rule_ids.length) return openRule(f.rule_ids[0], false);
    if (f.action === "add_placeholders") return $("#placeholders").click();
    if (f.action === "generate") return $("#add-rule-toggle").click();
  }

  // ---------------------------------------------------------------- rendering
  function render() {
    const draft = detail && detail.mapping.status === "DRAFT";
    const opt = detail && $("#mapping-select").selectedOptions[0];   // keep the dropdown label in step with the loaded set
    if (opt) opt.textContent = "#" + detail.mapping.mapping_set_id + " v" + detail.mapping.version + " - " + detail.mapping.status +
      (detail.mapping.archived ? " [archived]" : "") + " (" + detail.rules.length + " rules)";
    const archived = !!(detail && detail.mapping.archived);
    $("#archive-mapping").textContent = archived ? "Restore" : "Archive";
    $("#archive-mapping").disabled = !detail || detail.mapping.status === "SUBMITTED";
    $("#mapping-status").innerHTML = detail ? statusBadge(detail.mapping.status) + (archived ? " " + badge("gray", "archived: " + detail.mapping.archive_reason) : "") +
      (detail.mapping.approved_by ? ' <span class="muted">by ' + esc(detail.mapping.approved_by) + "</span>" : "") : "";
    $("#submit-mapping").disabled = !(detail && draft);
    $("#approve-mapping").disabled = !(detail && detail.mapping.status === "SUBMITTED");
    $("#run-pipeline").disabled = !(detail && detail.mapping.status !== "REJECTED" && !archived);
    $("#preflight").disabled = !detail;
    $("#add-rule-toggle").disabled = !draft;
    renderTarget();
    $("#placeholders").disabled = !draft;
    renderLegacy(); renderRules(); renderPreflight();
  }

  function renderLegacy() {
    if (!detail) { $("#legacy").innerHTML = '<p class="muted">No mapping set yet.</p>'; return; }
    $("#legacy").innerHTML = detail.source_columns.map((c) =>
      '<div class="legacy-col' + (filter === c.column_name ? " active" : "") + '" data-col="' + esc(c.column_name) + '" tabindex="0">' +
      '<span class="name">' + esc(c.column_name) + "</span> " + (c.missing_from_file ? badge("red", "not in the file") : "") + " " + (c.mapped ? badge("green", "mapped") : badge("gray", "unmapped")) +
      (!c.mapped && !c.missing_from_file && detail.mapping.status === "DRAFT" ? ' <button class="btn small suggest" data-col="' + esc(c.column_name) + '" title="Ask the AI for a rule for this column">Suggest rule</button>' : "") +
      "<small>" + esc(c.data_type) + " | nulls " + esc(pct(c.null_ratio, 0)) + " | " + esc(c.sample_values.join(", ")) + "</small></div>").join("");
    document.querySelectorAll(".legacy-col .suggest").forEach((btn) => btn.addEventListener("click", (e) => { e.stopPropagation(); openSuggest(btn.dataset.col); }));
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
    const missing = new Set(detail.source_columns.filter((c) => c.missing_from_file).map((c) => c.column_name));
    box.innerHTML = rules.map((r) => { const chk = ruleCheck(r); return (
      '<div class="rule" data-rule="' + esc(r.rule_id) + '"><div class="rule-head">' +
      "<div>" + (r.source_column ? "<strong>" + esc(r.source_column) + "</strong>" : '<span class="muted">(constant)</span>') + "</div>" +
      '<div class="arrow">&rarr;</div>' +
      "<div><strong>" + esc(r.target_table) + "." + esc(r.target_column) + "</strong>" + (r.target_mandatory ? ' <span title="mandatory">*</span>' : "") +
      '<div class="muted">' + esc(r.target_data_type) + (r.target_length ? "(" + esc(r.target_length) + ")" : "") + "</div></div>" +
      "<div>" + (r.confidence === null ? "" : confBadge(r.confidence) + " ") + (r.ai_suggested ? badge("purple", "AI-suggested") : "") + " " + ruleStatus(r) +
      (missing.has(r.source_column) ? " " + badge("red", "source column not in the file") : "") +
      (chk && chk.pass_rate !== null ? " " + badge(chk.verdict === "BLOCKER" ? "red" : chk.verdict === "WARNING" ? "amber" : "green",
        "sample: " + pct(chk.pass_rate, chk.pass_rate === 1 || chk.pass_rate === 0 ? 0 : 1) + " pass") : "") +
      (editable ? ' <button class="btn small edit">Edit</button> <button class="btn small delete">Delete</button>' : "") + "</div></div>" +
      '<div class="rule-meta"><span class="badge gray">' + esc(r.rule_type) + "</span>" +
      (r.transformation_logic ? ' <code>' + esc(r.transformation_logic) + "</code>" : "") +
      (r.rule_type === "VALUE_MAPPING" ? " " + esc(r.lookup_count) + " lookup values" : "") +
      (r.reasoning ? "<div>" + esc(r.reasoning) + "</div>" : "") +
      (r.review_reasons.length ? "<div>" + r.review_reasons.map((x) => badge("amber", x)).join(" ") + "</div>" : "") +
      (chk && chk.unmatched_values.length ? "<div>No lookup entry for: " + chk.unmatched_values.map((u) => "<code>" + esc(u.value) + "</code> (" + esc(App.num(u.count)) + ")").join(", ") + "</div>" : "") +
      (r.ai_suggested && r.ai_model_name ? '<div class="muted">model ' + esc(r.ai_model_name) + "</div>" : "") +
      "</div><div class=\"rule-edit hidden\"></div></div>"); }).join("") || '<p class="muted">No rules' + (filter ? " for this column" : "") + ".</p>";

    box.querySelectorAll(".rule").forEach((el) => {
      const btn = el.querySelector(".edit");
      if (btn) btn.addEventListener("click", () => toggleEditor(el, detail.rules.find((x) => x.rule_id === Number(el.dataset.rule))));
      const del = el.querySelector(".delete");
      if (del) del.addEventListener("click", () => {
        const rule = detail.rules.find((x) => x.rule_id === Number(el.dataset.rule));
        if (!window.confirm("Delete the rule for " + rule.target_table + "." + rule.target_column + "? This is recorded in the audit log.")) return;
        run(async () => {
          await api("DELETE", "/api/mapping/rules/" + rule.rule_id);
          App.toast("Rule deleted"); await loadDetail(detail.mapping.mapping_set_id);
        });
      });
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
        '<div class="wide"><button class="btn primary save">Save rule</button>' +
        (rule.rule_type === "VALUE_MAPPING" && rule.source_column ? ' <button class="btn draft-lookups" title="The AI drafts the translations from the values in your file; you approve each one">Draft lookups with AI</button>' : "") + "</div>" +
        '<div class="wide lookup-draft"></div></div>';
      box.querySelector(".save").addEventListener("click", () => saveRule(box, rule));
      const dl = box.querySelector(".draft-lookups");
      if (dl) dl.addEventListener("click", () => draftLookups(box, rule));
      showSavedDraft(box, rule);
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

  // ---------------------------------------------------------------- AI: suggest a rule for one column
  function openSuggest(column) {
    const panel = $("#suggest-panel");
    panel.classList.remove("hidden");
    panel.innerHTML = "<h2>AI suggestion for " + esc(column) + '</h2><div class="grid form"><label class="wide">What do you know about this column? (optional)<input id="sg-hint" maxlength="300" placeholder="e.g. this is the customer account group"></label>' +
      '<label>Ask the model this many times<select id="sg-runs"><option>1</option><option>2</option><option selected>3</option><option>5</option></select></label>' +
      '<div class="wide"><button class="btn primary" id="sg-run">Ask the AI</button> <button class="btn" id="sg-close">Close</button></div></div><div id="sg-result"></div>';
    panel.scrollIntoView({ block: "nearest" });
    $("#sg-close").addEventListener("click", () => panel.classList.add("hidden"));
    $("#sg-run").addEventListener("click", () => run(async () => {
      $("#sg-run").disabled = true; $("#sg-result").innerHTML = '<p class="muted"><span class="spinner"></span> Asking the model and measuring its answer against your file...</p>';
      try {
        const body = { column, runs: Number($("#sg-runs").value) }; const hint = $("#sg-hint").value.trim(); if (hint) body.hint = hint;
        renderSuggestion(await api("POST", "/api/mapping/" + detail.mapping.mapping_set_id + "/suggest-rule", body), column);
      } catch (e) { $("#sg-result").innerHTML = ""; throw e; } finally { $("#sg-run").disabled = false; }
    }));
  }

  function renderSuggestion(s, column) {
    const box = $("#sg-result"), ev = s.evidence;
    if (s.outcome === "NO_MATCH") { box.innerHTML = '<p>' + badge("gray", "no match") + " " + esc(s.reasoning || "The model found no suitable SAP field.") + "</p>"; return; }
    if (s.outcome === "ERROR") { box.innerHTML = "<p>" + badge("red", "no usable answer") + " " + esc(s.review_reasons.join("; ")) + "</p>"; return; }
    box.innerHTML = '<div class="rule"><div class="rule-head"><div><strong>' + esc(column) + '</strong></div><div class="arrow">&rarr;</div><div><strong>' + esc(s.target_table) + "." + esc(s.target_column) + "</strong></div>" +
      "<div>" + confBadge(s.confidence) + " " + badge("gray", s.rule_type) + " " + (s.acceptable ? "" : badge("red", "cannot be stored")) + "</div></div>" +
      '<div class="rule-meta">' + (s.transformation_sql ? "<code>" + esc(s.transformation_sql) + "</code>" : "") + "<div>" + esc(s.reasoning || "") + "</div>" +
      (s.review_reasons.length ? "<div>" + s.review_reasons.map((x) => badge("amber", x)).join(" ") + "</div>" : "") +
      (ev ? "<div>Measured on your file: " + esc(App.num(ev.non_blank)) + " of " + esc(App.num(ev.rows)) + " rows have a value, " + esc(App.num(ev.distinct)) + " distinct" +
        (ev.domain_valid_rate !== null ? "; " + esc(pct(ev.domain_valid_rate, 0)) + " already hold valid " + esc(ev.domain) + " keys" + (ev.top_invalid.length ? " (not valid: " + ev.top_invalid.slice(0, 3).map((x) => "<code>" + esc(x[0]) + "</code> " + esc(App.num(x[1]))).join(", ") + ")" : "") : "") +
        (ev.length_fit_rate !== null ? "; " + esc(pct(ev.length_fit_rate, 0)) + " fit the " + esc(ev.target_length) + " characters of the target" : "") + ".</div>" : "") +
      '<div class="muted">model ' + esc(s.model_name) + ", asked " + esc(s.runs) + " time(s), " + esc(pct(s.agreement, 0)) + " agreed" + (s.alternatives.length ? "; the others said " + esc(s.alternatives.join(", ")) : "") + "</div></div></div>" +
      (s.acceptable ? '<p><button class="btn primary" id="sg-accept">Accept as a rule (it still needs a review)</button></p>' : "");
    const accept = $("#sg-accept");
    if (accept) accept.addEventListener("click", () => run(async () => {
      const rule = await api("POST", "/api/mapping/" + detail.mapping.mapping_set_id + "/suggestions/" + s.suggestion_id + "/accept");
      App.toast("Rule added for " + rule.source_column + (rule.rule_type === "VALUE_MAPPING" ? ": now draft its lookups from the rule's Edit panel" : ""));
      $("#suggest-panel").classList.add("hidden"); await loadDetail(detail.mapping.mapping_set_id);
    }));
  }

  // ---------------------------------------------------------------- AI: draft the lookups of a value-mapping rule
  async function showSavedDraft(box, rule) {
    if (!rule.source_column || rule.rule_type !== "VALUE_MAPPING") return;
    const saved = await api("GET", "/api/mapping/rules/" + rule.rule_id + "/lookup-draft").catch(() => null);
    if (saved) renderDraft(box, rule, saved);
  }

  function draftLookups(box, rule) {
    run(async () => {
      const target = box.querySelector(".lookup-draft"), btn = box.querySelector(".draft-lookups");
      btn.disabled = true; target.innerHTML = '<p class="muted"><span class="spinner"></span> Looking at the most frequent values in your file; the AI is asked only about the ones it cannot be certain of...</p>';
      try { renderDraft(box, rule, await api("POST", "/api/mapping/rules/" + rule.rule_id + "/lookup-draft", {})); }
      catch (e) { target.innerHTML = ""; throw e; } finally { btn.disabled = false; }
    });
  }

  const METHOD = { valid_key: "already a valid key", synonym_list: "synonym list", ai: "AI", none: "no proposal" };
  function renderDraft(box, rule, d) {
    const cov = d.coverage, target = box.querySelector(".lookup-draft");
    const covText = (n) => (cov.non_blank_rows ? pct(n / cov.non_blank_rows, 1) : "-");
    target.innerHTML = '<h2>Drafted lookups for ' + esc(d.target) + "</h2>" +
      '<p class="muted">Keys are checked against ' + esc(d.domain_note) + " (" + esc(d.domain_size) + " keys): a proposed key that is not on that list is discarded. Tick what you approve; nothing is written until you apply. Draft by " + esc(d.created_by) + ", model " + esc(d.model_name) + ".</p>" +
      "<p>Rows covered by lookups now: <strong>" + esc(covText(cov.covered_now)) + "</strong>; with the pre-ticked entries: <strong>" + esc(covText(cov.covered_if_suggested)) + "</strong>; with every proposal: " + esc(covText(cov.covered_if_all)) +
      (d.not_drafted ? ' <span class="muted">(' + esc(App.num(d.not_drafted)) + " rarer values were not drafted)</span>" : "") + "</p>" +
      '<table class="draft"><thead><tr><th></th><th>Legacy value</th><th>Rows</th><th>Proposed key</th><th>How</th><th>Why</th></tr></thead><tbody>' + d.entries.map((e, i) =>
        "<tr><td>" + (e.status === "approved" ? badge("green", "approved") : e.proposed ? '<input type="checkbox" class="pick" data-i="' + i + '"' + (e.suggested_accept ? " checked" : "") + ">" : "") + "</td><td><code>" + esc(e.legacy_value) + "</code></td><td>" + esc(App.num(e.rows)) + "</td>" +
        "<td>" + (e.status === "approved" ? "<strong>" + esc(e.approved_key) + "</strong>" : '<input class="key" data-i="' + i + '" size="6" maxlength="10" value="' + esc(e.proposed || "") + '" placeholder="key">') + "</td>" +
        "<td>" + badge(e.method === "none" ? "gray" : e.method === "ai" ? (e.agreement && e.confidence >= 0.85 ? "purple" : "amber") : "green", METHOD[e.method]) + (e.method === "ai" ? " " + esc(Math.round(e.confidence * 100)) + "%" : "") + "</td><td>" + esc(e.reason) + "</td></tr>").join("") +
      '</tbody></table><p><button class="btn primary apply-draft">Apply the ticked entries</button></p>';
    target.querySelector(".apply-draft").addEventListener("click", () => run(async () => {
      const accept = [], overrides = {};
      target.querySelectorAll(".pick:checked").forEach((c) => {
        const e = d.entries[Number(c.dataset.i)], typed = target.querySelector('.key[data-i="' + c.dataset.i + '"]').value.trim();
        accept.push(e.legacy_value); if (typed && typed !== e.proposed) overrides[e.legacy_value] = typed;
      });
      if (!accept.length) { App.toast("Tick at least one entry", true); return; }
      const r = await api("POST", "/api/mapping/rules/" + rule.rule_id + "/lookup-draft/apply", { accept, overrides });
      App.toast(r.applied + " lookup(s) written, approved by " + r.approved_by + "; " + (r.coverage.non_blank_rows ? pct(r.coverage.covered_now / r.coverage.non_blank_rows, 1) : "-") + " of the rows are now covered");
      await loadDetail(detail.mapping.mapping_set_id);
    }));
  }

  // ---------------------------------------------------------------- actions
  App.studioRefresh = (id) => loadSets(id || (detail && detail.mapping.mapping_set_id));
  App.studioScope = () => ({ mapping_set_id: detail ? detail.mapping.mapping_set_id : null });
  $("#mapping-select").addEventListener("change", (e) => { if (e.target.value) loadDetail(Number(e.target.value)); });
  $("#show-archived").addEventListener("change", () => loadSets(detail && detail.mapping.mapping_set_id));
  $("#archive-mapping").addEventListener("click", () => run(async () => {
    const id = detail.mapping.mapping_set_id;
    if (detail.mapping.archived) {
      await api("POST", "/api/mapping/" + id + "/unarchive"); App.toast("Mapping set #" + id + " restored"); await loadSets(id); return;
    }
    const reason = window.prompt("Why is mapping set #" + id + " obsolete? (kept in the record; it can be restored)");
    if (!reason || reason.trim().length < 3) return;
    await api("POST", "/api/mapping/" + id + "/archive", { reason: reason.trim() });
    App.toast("Mapping set #" + id + " archived"); await loadSets();
  }));

  // ---------------------------------------------------------------- background AI-mapping job
  let pollTimer = null, currentJob = null;
  const ACTIVE = ["queued", "running", "cancelling"];
  const fmtElapsed = (job) => {
    const end = job.finished_at ? new Date(job.finished_at) : new Date();
    const sec = Math.max(0, Math.round((end - new Date(job.created_at)) / 1000));
    return Math.floor(sec / 60) + "m " + String(sec % 60).padStart(2, "0") + "s";
  };

  function showJob(job) {
    const active = ACTIVE.includes(job.status);
    $("#job").classList.toggle("hidden", !active);
    $("#generate").disabled = active;
    if (!active) return;
    const p = job.progress;
    $("#job-bar").max = Math.max(p.total, 1); $("#job-bar").value = p.current;
    $("#job-count").textContent = p.total ? p.current + " / " + p.total + " done" : "";
    $("#job-text").innerHTML = '<span class="spinner"></span> ' + esc(p.message);
    $("#job-time").textContent = "elapsed " + fmtElapsed(job);
    $("#job-cancel").disabled = job.status === "cancelling";
  }

  async function poll(jobId) {
    clearTimeout(pollTimer);
    let job;
    try { job = await api("GET", "/api/mapping/jobs/" + jobId); }
    catch (e) { $("#job").classList.add("hidden"); $("#generate").disabled = false; App.toast(e.message, true); return; }
    showJob(job);
    if (ACTIVE.includes(job.status)) { pollTimer = setTimeout(() => poll(jobId), 1500); return; }
    $("#generate").disabled = false;
    if (job.status === "succeeded") {
      const saved = job.result.report.proposals.filter((x) => x.outcome === "SAVED").length;
      App.toast("Draft mapping #" + job.result.mapping_set_id + ": " + saved + " rules proposed (" + fmtElapsed(job) + ")");
      await loadSets(job.result.mapping_set_id);
    } else if (job.status === "cancelled") App.toast("AI mapping cancelled; nothing was saved");
    else App.toast("AI mapping failed: " + (job.error ? job.error.code + " - " + job.error.detail : job.progress.message), true);
  }

  $("#generate").addEventListener("click", () => run(async () => {
    $("#generate").disabled = true;
    const runs = $("#consistency").value;
    const job = await api("POST", "/api/mapping/generate/" + tableId + "?background=true&partner_side=" + encodeURIComponent($("#side").value) + (runs ? "&consistency_runs=" + encodeURIComponent(runs) : ""));
    App.toast(job.already_running ? "An AI mapping job is already running; following it" : "AI mapping started");
    currentJob = job.job_id; showJob(job); poll(job.job_id);
  }));
  $("#job-cancel").addEventListener("click", () => run(async () => {
    if (currentJob) showJob(await api("POST", "/api/mapping/jobs/" + currentJob + "/cancel"));
  }));

  // resume following a job that is still running (e.g. after a page reload)
  (async () => {
    const jobs = (await run(() => api("GET", "/api/mapping/jobs?source_table_id=" + tableId))) || [];
    const active = jobs.find((j) => ACTIVE.includes(j.status));
    if (active) { currentJob = active.job_id; showJob(active); poll(active.job_id); }
  })();

  $("#new-set").addEventListener("click", () => run(async () => {
    const created = await api("POST", "/api/mapping-sets", { source_table_id: tableId });
    App.toast("Empty draft mapping set #" + created.mapping_set_id + " created - add rules below");
    await loadSets(created.mapping_set_id);
  }));
  $("#placeholders").addEventListener("click", () => run(async () => {
    const added = await api("POST", "/api/construction/placeholder-rules/" + detail.mapping.mapping_set_id);
    App.toast(added.length ? added.length + " placeholder rule(s) added: " + added.map((a) => a.target).join(", ") : "No mandatory field is missing a rule");
    await loadDetail(detail.mapping.mapping_set_id);
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
  $("#submit-mapping").addEventListener("click", () => run(async () => {
    try {
      await api("POST", "/api/governance/submit-mapping/" + detail.mapping.mapping_set_id);
    } catch (e) {
      if (e.code === "PREFLIGHT_BLOCKED" && e.detail && e.detail.report) {   // show what has to be fixed, right here
        preflight = e.detail.report; renderPreflight(); renderRules();
        $("#preflight-panel").scrollIntoView({ block: "nearest" });
        App.toast("Not submitted: " + e.detail.report.blockers + " problem(s) found by the check on a sample of your data. See the report above the rules.", true);
        return;
      }
      throw e;
    }
    App.toast("Submitted for approval"); await loadSets(detail.mapping.mapping_set_id);
  }));
  $("#preflight").addEventListener("click", () => run(async () => {
    const id = detail.mapping.mapping_set_id;
    $("#preflight").disabled = true; App.toast("Running the rules on a sample of the data...");
    try {
      preflight = await api("POST", "/api/mapping/" + id + "/preflight");
    } finally { $("#preflight").disabled = !detail; }
    renderPreflight(); renderRules();
    App.toast(preflight.can_submit ? "Check passed" + (preflight.warnings ? " with " + preflight.warnings + " warning(s)" : "") : "Check found " + preflight.blockers + " problem(s) to fix", !preflight.can_submit);
  }));
  $("#approve-mapping").addEventListener("click", () => govern("approve-mapping", "Mapping set approved"));

  $("#run-pipeline").addEventListener("click", () => run(async () => {
    $("#run-pipeline").disabled = true; App.toast("Running transform, deduplication and validation...");
    try {
      const r = await api("POST", "/api/pipeline/run/" + detail.mapping.mapping_set_id, {});
      App.open("/ui/governance/" + r.run_id, "Governance");
    } finally { $("#run-pipeline").disabled = false; }
  }));

  loadTargets();
  loadSets();
})();
