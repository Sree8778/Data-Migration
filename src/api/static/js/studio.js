(function () {
  "use strict";
  const { $, esc, api, run, badge, statusBadge, confBadge, pct } = App;
  const tableId = App.pageData("tableId");
  let detail = null, filter = null, sets = [], preflight = null, exclusions = null;

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
    const now = detail.rules.map((r) => r.rule_id + ":" + r.rule_type + ":" + r.lookup_count).sort().join(",");
    return preflight.rules.map((r) => r.rule_id + ":" + r.rule_type + ":" + r.lookup_count).sort().join(",") !== now;
  }
  const ruleCheck = (r) => (preflight && !preflightStale() ? preflight.rules.find((c) => c.rule_id === r.rule_id) : null);

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
      (p.models.length ? "; model " + esc(p.models.join(", ")) : "") + "</span></p>" +
      (p.findings.length ? '<ul class="findings">' + p.findings.map((f) => "<li>" + badge(f.severity === "BLOCKER" ? "red" : "amber", f.severity) + " " + esc(f.message) + "</li>").join("") + "</ul>"
        : '<p class="muted">Nothing to report.</p>');
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
    $("#placeholders").disabled = !draft;
    renderLegacy(); renderRules(); renderPreflight();
  }

  function renderLegacy() {
    if (!detail) { $("#legacy").innerHTML = '<p class="muted">No mapping set yet.</p>'; return; }
    $("#legacy").innerHTML = detail.source_columns.map((c) =>
      '<div class="legacy-col' + (filter === c.column_name ? " active" : "") + '" data-col="' + esc(c.column_name) + '" tabindex="0">' +
      '<span class="name">' + esc(c.column_name) + "</span> " + (c.missing_from_file ? badge("red", "not in the file") : "") + " " + (c.mapped ? badge("green", "mapped") : badge("gray", "unmapped")) +
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
    const job = await api("POST", "/api/mapping/generate/" + tableId + "?background=true" + (runs ? "&consistency_runs=" + encodeURIComponent(runs) : ""));
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

  loadSets();
})();
