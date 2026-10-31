/* Combine landed tables: the form only ever builds the structured description the server validates; no SQL is written here. */
(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge } = App;
  const ALIASES = "abcdefg".split("");
  const OPS = [["equals", "is"], ["not_equals", "is not"], ["in", "is one of (comma separated)"], ["contains", "contains"], ["starts_with", "starts with"],
    ["regex", "matches pattern"], ["is_blank", "is blank"], ["not_blank", "is not blank"]];
  let tables = [];
  const columns = {};                                      // table id -> [column names]
  const state = { base: null, joins: [], filters: [] };    // joins: {table_id, type, on:[{left,right}]}; filters: {column, op, value}

  const alias = (i) => ALIASES[i];                         // 0 = base, 1.. = joins
  const tableName = (id) => (tables.find((t) => t.table_id === id) || {}).table_name || "?";
  const options = (items, selected) => items.map(([v, label]) => '<option value="' + esc(v) + '"' + (v === selected ? " selected" : "") + ">" + esc(label) + "</option>").join("");
  const tableOptions = (selected) => options(tables.filter((t) => t.has_source_file).map((t) =>
    [String(t.table_id), t.table_name + " (#" + t.table_id + ", " + num(t.rows_landed) + " rows)"]), String(selected));

  async function ensureColumns(id) {
    if (columns[id]) return;
    const snap = await run(() => api("GET", "/api/tables/" + id + "/snapshot"));
    columns[id] = snap ? snap.columns.map((c) => c.name) : [];
  }
  const refsUpTo = (n) => {                                // alias.column of the base and the first n joins
    const out = [];
    for (let i = 0; i <= n; i++) {
      const id = i === 0 ? state.base : state.joins[i - 1].table_id;
      (columns[id] || []).forEach((c) => out.push([alias(i) + "." + c, alias(i) + "." + c + "  (" + tableName(id) + ")"]));
    }
    return out;
  };

  function render() {
    $("#base-table").innerHTML = tableOptions(state.base);
    $("#joins").innerHTML = state.joins.map((j, i) => {
      const left = refsUpTo(i), right = (columns[j.table_id] || []).map((c) => [alias(i + 1) + "." + c, c]);
      return '<div class="card" data-join="' + i + '"><div class="grid form">' +
        '<label>Join table <strong>' + alias(i + 1) + '</strong><select data-f="table">' + tableOptions(j.table_id) + "</select></label>" +
        '<label>Records without a partner<select data-f="type">' + options([["left", "stay, with empty columns (left join)"], ["inner", "are dropped (inner join)"]], j.type) + "</select></label>" +
        j.on.map((p, k) => '<label>Key ' + (k + 1) + ': a record of ... matches<select data-f="left" data-k="' + k + '">' + options(left, p.left) + "</select></label>" +
          '<label>... a record of ' + alias(i + 1) + ' where<select data-f="right" data-k="' + k + '">' + options(right, p.right) + "</select> is equal</label>").join("") +
        '<div class="wide"><button type="button" class="btn small" data-act="add-key">+ key</button> <button type="button" class="btn small" data-act="del-join">Remove this table</button></div></div></div>';
    }).join("");
    const all = refsUpTo(state.joins.length);
    $("#filters").innerHTML = state.filters.map((f, i) =>
      '<div class="toolbar" data-filter="' + i + '">Keep records where <select data-f="column">' + options(all, f.column) + "</select>" +
      '<select data-f="op">' + options(OPS, f.op) + "</select>" +
      (f.op === "is_blank" || f.op === "not_blank" ? "" : '<input data-f="value" value="' + esc(f.value) + '" placeholder="value">') +
      '<button type="button" class="btn small" data-act="del-filter">Remove</button></div>').join("");
  }

  function spec() {
    return {
      base: { table_id: state.base, alias: alias(0) },
      joins: state.joins.map((j, i) => ({ table_id: j.table_id, alias: alias(i + 1), type: j.type, on: j.on.map((p) => ({ left: p.left, right: p.right })) })),
      filters: state.filters.map((f) => ({ column: f.column, op: f.op,
        values: f.op === "is_blank" || f.op === "not_blank" ? [] : f.op === "in" ? f.value.split(",").map((v) => v.trim()).filter(Boolean) : [f.value] })),
      match: $("#match").value, allow_fanout: $("#allow-fanout").checked,
    };
  }

  $("#base-table").addEventListener("change", async (e) => {
    state.base = Number(e.target.value); state.joins = []; state.filters = []; await ensureColumns(state.base); render();
  });
  $("#add-join").onclick = async () => {
    const used = new Set([state.base, ...state.joins.map((j) => j.table_id)]);
    const next = tables.find((t) => t.has_source_file && !used.has(t.table_id)) || tables.find((t) => t.has_source_file);
    if (!next || state.joins.length >= 5) return App.toast("No more tables to add", true);
    await ensureColumns(next.table_id);
    const i = state.joins.length;
    state.joins.push({ table_id: next.table_id, type: "left", on: [{ left: (refsUpTo(i)[0] || [""])[0], right: alias(i + 1) + "." + (columns[next.table_id][0] || "") }] });
    render();
  };
  $("#add-filter").onclick = () => { state.filters.push({ column: refsUpTo(state.joins.length)[0][0], op: "equals", value: "" }); render(); };
  $("#joins").addEventListener("change", async (e) => {
    const card = e.target.closest("[data-join]"); if (!card) return;
    const i = Number(card.dataset.join), j = state.joins[i], f = e.target.dataset.f, k = Number(e.target.dataset.k || 0);
    if (f === "table") {
      j.table_id = Number(e.target.value); await ensureColumns(j.table_id);
      j.on = [{ left: (refsUpTo(i)[0] || [""])[0], right: alias(i + 1) + "." + (columns[j.table_id][0] || "") }];
    } else if (f === "type") j.type = e.target.value; else j.on[k][f] = e.target.value;
    render();
  });
  $("#joins").addEventListener("click", (e) => {
    const card = e.target.closest("[data-join]"), act = e.target.dataset.act; if (!card || !act) return;
    const i = Number(card.dataset.join), j = state.joins[i];
    if (act === "del-join") { state.joins.splice(i, 1); state.filters = []; }
    else if (act === "add-key" && j.on.length < 4) j.on.push({ left: refsUpTo(i)[0][0], right: alias(i + 1) + "." + columns[j.table_id][0] });
    render();
  });
  $("#filters").addEventListener("input", (e) => {
    const row = e.target.closest("[data-filter]"); if (row && e.target.dataset.f === "value") state.filters[Number(row.dataset.filter)].value = e.target.value;
  });
  $("#filters").addEventListener("change", (e) => {
    const row = e.target.closest("[data-filter]"); if (!row || e.target.dataset.f === "value") return;
    state.filters[Number(row.dataset.filter)][e.target.dataset.f] = e.target.value; render();
  });
  $("#filters").addEventListener("click", (e) => {
    const row = e.target.closest("[data-filter]"); if (row && e.target.dataset.act === "del-filter") { state.filters.splice(Number(row.dataset.filter), 1); render(); }
  });

  let previewed = null;
  $("#preview").onclick = () => run(async () => {
    previewed = null; $("#create-card").classList.add("hidden");
    let p;
    try { p = await api("POST", "/api/sources/combine/preview", Object.assign(spec(), { sample_rows: 10 })); }
    catch (e) { $("#result").innerHTML = '<div class="banner red">' + esc(e.message) + "</div>"; return; }
    const stats = '<div class="cards">' + App.stat("Records in the first table", num(p.base_rows)) + App.stat("Rows after joining", num(p.rows_after_joins)) +
      App.stat("Removed by filters", num(p.removed_by_filters)) + App.stat("Rows in the result", num(p.rows_out)) + "</div>";
    const joins = p.joins.length ? table(["Join", "Records looked up", "Table rows", "Find no partner", "Find several", "Most partners"],
      p.joins.map((j) => [esc(j.alias) + " = " + esc(j.table_name) + " (" + esc(j.type) + ")", num(j.left_rows), num(j.right_rows),
        j.left_rows_without_a_match ? badge("amber", num(j.left_rows_without_a_match)) : "0",
        j.left_rows_with_several ? badge("red", num(j.left_rows_with_several)) : "0", num(j.most_partners)])) : "";
    $("#result").innerHTML = (p.blocked ? '<div class="banner red">Cannot be created as described: ' + esc(p.blocked) + "</div>" : "") +
      p.warnings.map((w) => '<div class="banner amber">' + esc(w) + "</div>").join("") + stats + joins +
      "<h3>First rows</h3>" + table(p.columns.map((c) => c.name), p.sample.map((r) => r.map(esc))) +
      "<details><summary>What will run</summary><pre>" + esc(p.sql) + "</pre></details>";
    if (!p.blocked) { previewed = p; $("#create-card").classList.remove("hidden"); }
  });
  $("#create").onclick = () => run(async () => {
    const name = $("#c-table").value.trim();
    if (!previewed || !name) return App.toast("Preview first, and name the new table", true);
    const body = Object.assign(spec(), { system_name: $("#c-system").value.trim(), table_name: name,
      primary_key_columns: $("#c-keys").value.split(",").map((k) => k.trim()).filter(Boolean) });
    const r = await api("POST", "/api/sources/combine", body);
    $("#created").innerHTML = '<div class="banner">Created table #' + esc(r.table_id) + " with " + num(r.rows_landed) + ' rows. <a href="/ui/profiler/' + esc(r.table_id) +
      '">Profile it</a> or <a href="/ui/studio/' + esc(r.table_id) + '">map it</a>.</div>';
    App.toast("Table created");
  });

  (async () => {
    tables = (await run(() => api("GET", "/api/tables"))) || [];
    const usable = tables.filter((t) => t.has_source_file);
    if (!usable.length) { $("#base-table").innerHTML = ""; $("#joins").innerHTML = '<p class="muted">Land at least one table first (Sources).</p>'; return; }
    state.base = usable[0].table_id; await ensureColumns(state.base); render();
  })();
})();
