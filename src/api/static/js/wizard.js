/* Add Source / Add Target wizards: a fixed step list on the left, a description box, a body, and Back / Next / Cancel / Help.
   Every dynamic value that reaches innerHTML goes through App.esc(). Nothing is stored until Finish. */
(function () {
  "use strict";
  const { $, esc, api, run, num } = App;
  const dlg = $("#wizard");
  let wz = null;

  // ------------------------------------------------------------------ engine
  function open(def) {
    wz = Object.assign({ i: 0, busy: false, showHelp: false }, def, { state: def.init() });
    $("#wz-title").textContent = def.title;
    dlg.classList.toggle("has-guide", !!def.guide);
    if (def.guide && guide) guide.reset();
    showError("");
    renderStep();
    if (!dlg.open) dlg.showModal();
  }
  const close = () => { if (dlg.open) dlg.close(); wz = null; };
  const abort = () => { const w = wz; close(); if (w && w.cancel) w.cancel(w.state); };   // leave nothing behind that the user did not finish

  function showError(msg) {
    const el = $("#wz-error");
    el.textContent = msg || ""; el.classList.toggle("hidden", !msg);
  }

  function renderStep() {
    const step = wz.steps[wz.i];
    $("#wz-steps").innerHTML = wz.steps.map((s, k) =>
      '<li class="' + (k === wz.i ? "current" : k < wz.i ? "done" : "") + '">' + esc(s.label) + "</li>").join("");
    $("#wz-desc").innerHTML = esc(step.desc) + (wz.showHelp && step.help ? '<span class="more">' + esc(step.help) + "</span>" : "");
    const body = $("#wz-body");
    body.innerHTML = ""; body.classList.remove("wz-busy");   // a previous wizard's Finish must not leave the body unclickable
    step.render(body, wz.state, wz);
    refreshNav();
    syncGuide();
  }

  function refreshNav() {
    if (!wz) return;
    const last = wz.i === wz.steps.length - 1;
    $("#wz-back").disabled = wz.i === 0 || wz.busy;
    $("#wz-next").textContent = last ? "Finish" : "Next >";
    const gate = wz.steps[wz.i].gate ? wz.steps[wz.i].gate(wz.state) : null;   // a string = why Next is not available yet
    $("#wz-next").disabled = wz.busy || !!gate;
    $("#wz-next").title = gate || "";
  }
  wz_bind();

  function wz_bind() {
    $("#wz-back").addEventListener("click", () => { if (wz && wz.i > 0) { wz.i -= 1; showError(""); renderStep(); } });
    $("#wz-cancel").addEventListener("click", abort);
    $("#wz-close").addEventListener("click", abort);
    $("#wz-help").addEventListener("click", () => { if (wz) { wz.showHelp = !wz.showHelp; renderStep(); } });
    dlg.addEventListener("cancel", () => { const w = wz; wz = null; if (w && w.cancel) w.cancel(w.state); });
    $("#wz-next").addEventListener("click", async () => {
      if (!wz || wz.busy) return;
      const step = wz.steps[wz.i];
      const problem = step.check ? step.check(wz.state) : null;
      if (problem) { showError(problem); return; }
      showError("");
      if (wz.i < wz.steps.length - 1) { wz.i += 1; renderStep(); return; }
      wz.busy = true; refreshNav(); $("#wz-body").classList.add("wz-busy");
      try { await wz.finish(wz.state); close(); }
      catch (e) { showError(e.message || String(e)); wz.busy = false; $("#wz-body").classList.remove("wz-busy"); refreshNav(); }
    });
    dlg.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.matches("input[type=text], input[type=number]") && !$("#wz-next").disabled) { e.preventDefault(); $("#wz-next").click(); }
    });
  }

  // small builders
  const row = (label, control, hint) => '<div class="wz-row"><label>' + esc(label) + "</label>" + control + (hint ? '<div class="hint">' + esc(hint) + "</div>" : "") + "</div>";
  const options = (list, current) => list.map(([value, label, disabled]) =>
    '<option value="' + esc(value) + '"' + (value === current ? " selected" : "") + (disabled ? " disabled" : "") + ">" + esc(label) + "</option>").join("");
  const summary = (pairs) => '<table class="wz-sum"><tbody>' + pairs.filter(Boolean).map(([k, v]) => "<tr><td>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("") + "</tbody></table>";
  const bytes = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

  // ================================================================== ADD SOURCE
  const PROVIDERS = [["", "Auto-detect (recommended)"], ["delimited", "Delimited text (CSV, TSV, pipe)"], ["excel", "Excel workbook (.xlsx)"],
    ["json", "JSON / NDJSON"], ["fixed", "Fixed-width text"], ["parquet", "Parquet"]];
  const COMING = ["Oracle Database", "Microsoft SQL Server", "SAP ECC (tables)", "PostgreSQL", "MySQL / MariaDB"];

  function defaultTableName(filename) {
    let base = filename.replace(/\.(gz|zip)$/i, "").replace(/\.[^.]+$/, "").toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (!base) base = "SOURCE_TABLE";
    return /^[0-9]/.test(base) ? "T_" + base : base;
  }

  function apiOptions(st) {
    const o = { header: st.opt.header !== false }, set = (k, v) => { if (v !== "" && v !== null && v !== undefined && !Number.isNaN(v)) o[k] = v; };
    set("format", st.opt.format || ""); set("delimiter", st.opt.delimiter || ""); set("encoding", (st.opt.encoding || "").trim());
    set("sheet", st.opt.sheet || ""); set("member", st.opt.member || "");
    if (Number(st.opt.header_row) > 1) o.header_row = Number(st.opt.header_row);
    if (Number(st.opt.skip_rows) > 0) o.skip_rows = Number(st.opt.skip_rows);
    const tokens = (st.opt.nulls || "").split(",").map((t) => t.trim()).filter(Boolean);
    if (tokens.length) o.null_tokens = tokens;
    const layout = (st.opt.layout || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => l.split(","));
    if (layout.length) o.columns = layout.map(([name, start, width]) => ({ name: (name || "").trim(), start: Number(start), width: Number(width) }));
    return o;
  }

  async function readFile(st, statusEl, then) {
    if (!st.file) return;
    statusEl.className = "wz-status"; statusEl.textContent = "Reading the file...";
    const form = new FormData();
    form.append("file", st.file); form.append("options", JSON.stringify(apiOptions(st)));
    try {
      const p = await api("POST", "/api/sources/preview", form, true);
      st.preview = p;
      const o = p.options || {};          // the resolved options come back with the detection results filled in
      st.opt.format = o.format || st.opt.format || ""; st.opt.delimiter = o.delimiter || ""; st.opt.encoding = o.encoding || "";
      st.opt.sheet = o.sheet || ""; st.opt.member = o.member || "";
      st.detected = (p.format ? "format " + p.format : "") + (p.encoding ? ", encoding " + p.encoding : "") +
        (p.delimiter ? ", delimiter " + (p.delimiter === "\t" ? "tab" : p.delimiter) : "") + " - " + p.columns.length + " columns";
      if (!st.table) st.table = defaultTableName(st.file.name);
      statusEl.className = "wz-status ok"; statusEl.textContent = "Test succeeded: " + st.detected + (p.warnings.length ? " (" + p.warnings.length + " warning(s))" : "");
      st.lastError = "";
    } catch (e) {
      st.preview = null; st.lastError = e.message;
      statusEl.className = "wz-status bad"; statusEl.textContent = e.message;
    }
    if (then) then();
    refreshNav();
  }

  const fileSteps = [
    {
      label: "Select provider", desc: "Select the kind of system that holds the source data and give the source a name.",
      help: "The name groups tables from the same legacy system, for example LEGACY_ORACLE_AR. Database connections are planned; today a source is an extract file.",
      render(el, st) {
        const systems = [...new Set(window.Shell.tables().map((t) => t.system_name))].sort();
        el.innerHTML = '<fieldset><legend>Source name</legend>' +
          row("Name:", '<input type="text" id="s-name" maxlength="128" list="s-systems" value="' + esc(st.name) + '" autocomplete="off" placeholder="LEGACY_ORACLE_AR">' +
            '<datalist id="s-systems">' + systems.map((s) => '<option value="' + esc(s) + '">').join("") + "</datalist>",
            systems.length ? "Pick an existing system to add a table to it, or type a new name." : "") + "</fieldset>" +
          '<fieldset><legend>Data Provider(s)</legend>' +
          row("Database:", '<select id="s-kind"><optgroup label="Files"><option value="file"' + (st.kind === "file" ? " selected" : "") + ">Extract file (CSV, Excel, JSON, fixed-width, Parquet)</option></optgroup>" +
            '<optgroup label="Databases (read-only)">' + kindOptions(st, st.kind) + '</optgroup><optgroup label="Cloud file storage (read-only)">' + storageOptions(st) + "</optgroup></select>") +
          (st.kind === "file"
            ? row("Provider:", '<select id="s-provider">' + options(PROVIDERS, st.opt.format || "") + "</select>", "Leave on auto-detect unless the file is read wrongly.")
            : row("Provider:", '<input type="text" readonly value="' + esc(DRIVERS[st.kind] || "") + '">', ((st.presets || []).find((x) => x.id === st.preset) || {}).hint || "The server connects with this driver. Nothing is installed on the legacy system; use a read-only account.")) + "</fieldset>";
        $("#s-name").addEventListener("input", (e) => { st.name = e.target.value; if (st.kind !== "file") { st.dbtest = null; st.connSaved = false; } refreshNav(); });
        $("#s-kind").addEventListener("change", (e) => { const v = e.target.value; if (v === "file") { st.kind = "file"; st.preset = null; } else applyPreset(st, v); st.preview = null; st.dbtest = null; renderStep(); });
        const provider = $("#s-provider");
        if (provider) provider.addEventListener("change", (e) => { st.opt.format = e.target.value; st.preview = null; });
        $("#s-name").focus();
      },
      gate: (st) => (!st.name.trim() ? "Enter a name for the source" : st.kind !== "file" && !/^[A-Za-z0-9_.-]{1,64}$/.test(st.name.trim()) ? "A database source name may use letters, digits, '.', '_' and '-' only (up to 64)" : null),
    },
    {
      label: "Set connection", desc: "Choose the extract file. The format, encoding and delimiter are detected; correct anything that looks wrong and press Test.",
      help: "Test reads the first rows of the file without storing anything. The control total (next step but one) lets the system prove that every row arrived.",
      render(el, st) {
        const p = st.preview, fmt = (p && p.format) || st.opt.format || "";
        const prop = (key, label, control, help) => '<tr data-key="' + key + '" data-help="' + esc(label + "|" + help) + '"><td>' + esc(label) + "</td><td>" + control + "</td></tr>";
        let req = prop("file", "Extract file", '<input type="file" id="p-file" accept=".csv,.tsv,.txt,.psv,.dat,.xlsx,.json,.ndjson,.jsonl,.parquet,.gz,.zip">',
          "The legacy extract. Delimited text, Excel, JSON / NDJSON, fixed-width or Parquet; optionally .gz or .zip.");
        let opt = prop("format", "Format", '<select id="p-format">' + options(PROVIDERS, st.opt.format || "") + "</select>", "How the file is structured. Auto-detect looks at the file itself.");
        if (!p || fmt === "delimited") {
          opt += prop("delimiter", "Delimiter", '<select id="p-delimiter">' + options([["", "auto-detect"], [",", "comma ,"], [";", "semicolon ;"], ["\t", "tab"], ["|", "pipe |"]], st.opt.delimiter || "") + "</select>", "The character that separates columns.");
          opt += prop("encoding", "Encoding", '<input type="text" id="p-encoding" value="' + esc(st.opt.encoding || "") + '" placeholder="auto (utf-8, cp1252, utf-16...)">', "Text encoding. Legacy Windows exports are often cp1252; accents show wrongly when this is wrong.");
        }
        if (p && fmt === "json") opt += prop("encoding", "Encoding", '<input type="text" id="p-encoding" value="' + esc(st.opt.encoding || "") + '" placeholder="auto">', "Text encoding of the JSON file.");
        if (p && p.sheets && p.sheets.length) {
          opt += prop("sheet", "Excel sheet", '<select id="p-sheet"><option value="">first visible sheet</option>' + p.sheets.map((s) => '<option' + (s === st.opt.sheet ? " selected" : "") + ">" + esc(s) + "</option>").join("") + "</select>", "Which worksheet holds the data.");
          opt += prop("header_row", "Header row", '<input type="number" id="p-header-row" min="1" value="' + esc(st.opt.header_row || 1) + '">', "The row number that contains the column names (banner rows above it are skipped).");
        }
        if (p && p.members && p.members.length) opt += prop("member", "File inside zip", '<select id="p-member"><option value="">(only one file)</option>' + p.members.map((s) => '<option' + (s === st.opt.member ? " selected" : "") + ">" + esc(s) + "</option>").join("") + "</select>", "The file to read from the archive.");
        if (fmt !== "excel" && fmt !== "parquet") opt += prop("skip_rows", "Skip leading lines", '<input type="number" id="p-skip" min="0" value="' + esc(st.opt.skip_rows || 0) + '">', "Banner or comment lines before the header.");
        if (fmt !== "parquet") opt += prop("header", "First row is header", '<input type="checkbox" id="p-header"' + (st.opt.header !== false ? " checked" : "") + ">", "Untick when the file has no column names.");
        opt += prop("nulls", "Also read as empty", '<input type="text" id="p-nulls" value="' + esc(st.opt.nulls || "") + '" placeholder="NULL, \\N">', "Extra values that mean 'no value', comma separated. Blank cells are always empty.");
        if (fmt === "fixed" || st.opt.format === "fixed") opt += prop("layout", "Column layout", '<textarea id="p-layout" spellcheck="false" placeholder="NAME,start,width (one column per line)">' + esc(st.opt.layout || "") + "</textarea>", "Fixed-width files need a layout: column name, 1-based start position and width, one per line.");
        el.innerHTML = '<div class="pg-wrap"><table class="pg"><tbody><tr class="grp"><td colspan="2">&#9662; Required</td></tr>' + req +
          '<tr class="grp"><td colspan="2">&#9662; Optional</td></tr>' + opt + "</tbody></table></div>" +
          '<div class="pg-help" id="pg-help"><strong>Extract file</strong>The legacy extract. Choose the file to read.</div>' +
          '<div class="wz-actionrow"><span class="wz-status" id="p-status">' + (st.preview ? "Test succeeded: " + esc(st.detected) : st.file ? "Press Test to read the file." : "No file chosen.") + "</span>" +
          '<button type="button" class="wz-btn" id="p-test"' + (st.file ? "" : " disabled") + ">Test</button></div>";
        if (st.preview) $("#p-status").classList.add("ok");
        const status = $("#p-status");
        const bind = (id, key, ev, get) => { const c = $(id); if (c) c.addEventListener(ev || "change", () => { st.opt[key] = get ? get(c) : c.value; st.preview = st.preview; }); };
        bind("#p-format", "format"); bind("#p-delimiter", "delimiter"); bind("#p-encoding", "encoding", "input"); bind("#p-sheet", "sheet"); bind("#p-member", "member");
        bind("#p-header-row", "header_row"); bind("#p-skip", "skip_rows"); bind("#p-nulls", "nulls", "input"); bind("#p-layout", "layout", "input");
        bind("#p-header", "header", "change", (c) => c.checked);
        el.querySelectorAll("tr[data-key]").forEach((tr) => tr.addEventListener("focusin", () => {
          el.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel");
          const [title, text] = tr.dataset.help.split("|"); $("#pg-help").innerHTML = "<strong>" + esc(title) + "</strong>" + esc(text);
        }));
        const rerender = () => renderStep();
        $("#p-file").addEventListener("change", (e) => {
          st.file = e.target.files[0] || null; st.preview = null; st.table = ""; st.keys = new Set();
          if (st.file) readFile(st, status, rerender); else rerender();
        });
        $("#p-test").addEventListener("click", () => readFile(st, status, rerender));
        if (st.file) { const dt = new DataTransfer(); dt.items.add(st.file); $("#p-file").files = dt.files; }   // keep the chosen file when stepping back and forth
      },
      gate: (st) => (!st.file ? "Choose an extract file" : !st.preview ? "Press Test to read the file" : null),
    },
    {
      label: "Select table", desc: "Name the table that this file becomes, and choose the column(s) that identify a record.",
      help: "The primary key is optional but recommended: a key that repeats or is blank is rejected, so duplicates are caught at the door. The control total is the row count your legacy team reports for the extract.",
      render(el, st) {
        const p = st.preview;
        el.innerHTML = '<fieldset><legend>Table</legend>' +
          row("Table name:", '<input type="text" id="t-name" maxlength="128" value="' + esc(st.table) + '">', "Registering the same name again replaces the earlier snapshot.") +
          row("Business name:", '<input type="text" id="t-business" maxlength="256" value="' + esc(st.business) + '" placeholder="optional, e.g. AR customer master">') +
          row("Control total:", '<input type="number" id="t-expected" min="0" value="' + esc(st.expected) + '" placeholder="optional: rows the source system reports">',
            "When given, the file is rejected unless exactly this many rows are read.") + "</fieldset>" +
          '<fieldset><legend>Primary key column(s)</legend><div class="wz-cols"><table><thead><tr><th>Key</th><th>Column</th><th>Sample values</th></tr></thead><tbody>' +
          p.columns.map((c, k) => '<tr><td><input type="checkbox" class="t-key" data-col="' + esc(c) + '"' + (st.keys.has(c) ? " checked" : "") + '></td><td>' + esc(c) + "</td><td><code>" +
            esc(p.rows.slice(0, 3).map((r) => (r[k] === null ? "null" : r[k])).join(", ")) + "</code></td></tr>").join("") + "</tbody></table></div></fieldset>";
        $("#t-name").addEventListener("input", (e) => { st.table = e.target.value; refreshNav(); });
        $("#t-business").addEventListener("input", (e) => { st.business = e.target.value; });
        $("#t-expected").addEventListener("input", (e) => { st.expected = e.target.value; });
        el.querySelectorAll(".t-key").forEach((c) => c.addEventListener("change", () => { if (c.checked) st.keys.add(c.dataset.col); else st.keys.delete(c.dataset.col); }));
      },
      gate: (st) => (st.table.trim() ? null : "Enter a table name"),
    },
    {
      label: "Actions", desc: "Choose the action(s) to perform at the end of the wizard.",
      render(el, st) {
        el.innerHTML = '<div class="wz-checks"><div>At the end of the wizard:</div>' +
          '<label><input type="checkbox" id="a-profile"' + (st.profile ? " checked" : "") + '> Profile the table now</label><div class="sub">Counts nulls, distinct values and duplicates so the Profiler is ready.</div>' +
          '<label><input type="checkbox" id="a-studio"' + (st.studio ? " checked" : "") + '> Open the Mapping Studio</label><div class="sub">Continue straight to mapping the legacy columns to SAP fields.</div>' +
          '<label><input type="checkbox" id="a-target"' + (st.target ? " checked" : "") + '> Launch the Add Target Connection Wizard</label><div class="sub">Check where the migrated data will be loaded.</div></div>';
        [["#a-profile", "profile"], ["#a-studio", "studio"], ["#a-target", "target"]].forEach(([id, k]) => $(id).addEventListener("change", (e) => { st[k] = e.target.checked; }));
      },
    },
    {
      label: "Summary", desc: "Check that the source contains the correct values.",
      render(el, st) {
        const p = st.preview, keys = [...st.keys];
        el.innerHTML = '<fieldset><legend>Source summary</legend>' + summary([
          ["Source name", st.name.trim()], ["Provider", "Extract file (" + (p.format || "auto") + ")"], ["File", st.file.name + " (" + bytes(st.file.size) + ")"],
          ["Encoding", p.encoding ? p.encoding + (p.encoding_basis ? " - " + p.encoding_basis : "") : "n/a"], p.delimiter ? ["Delimiter", p.delimiter === "\t" ? "tab" : p.delimiter] : null,
          p.sheets && p.sheets.length ? ["Sheet", st.opt.sheet || "first visible sheet"] : null,
          ["Table", st.table.trim() + (st.business.trim() ? " (" + st.business.trim() + ")" : "")], ["Columns", num(p.columns.length)],
          ["Primary key", keys.length ? keys.join(", ") : "none"], ["Control total", st.expected !== "" ? num(Number(st.expected)) + " rows" : "not provided"],
        ]) + "</fieldset>" +
          '<fieldset><legend>Click Finish to perform the following actions</legend>' + summary([
            ["", "Land the file as a snapshot and register the table"], st.profile ? ["", "Profile the table"] : null, st.studio ? ["", "Open the Mapping Studio"] : null, st.target ? ["", "Launch the Add Target Connection Wizard"] : null]) + "</fieldset>" +
          (p.warnings.length ? '<div class="banner amber">' + p.warnings.map(esc).join("<br>") + "</div>" : "");
      },
    },
  ];

  // ------------------------------------------------------------------ database sources
  const DRIVERS = { azure_blob: "azure-storage-blob (Blob endpoint; Data Lake Gen2 works through it)", s3: "boto3 (any S3-compatible service)", sftp: "paramiko (host key must be confirmed)", postgresql: "psycopg2 (native PostgreSQL driver)", oracle: "python-oracledb (thin mode, no Oracle client needed)", mssql: "python-tds (TDS protocol, no ODBC driver needed)", mysql: "PyMySQL", firestore: "google-cloud-firestore (one document = one row; sub-collections are not followed; Firestore bills one read per document)", mongodb: "pymongo (documents are flattened into rows; read-only)" };
  const enc = encodeURIComponent;
  const newConn = () => ({ host: "", port: "", database: "", user: "", password: "", password_env: "", timeout: "", opts: {} });

  function connBody(st) {
    const c = st.conn, options = {};
    if (c.timeout) options.connect_timeout = Number(c.timeout);
    Object.entries(c.opts || {}).forEach(([k, v]) => { if (v !== "" && v !== undefined && v !== null) options[k] = v; });   // encryption settings per database
    const d = st.dialects.find((x) => x.dialect === st.kind) || {};
    if (st.kind === "firestore") {          // the real service has one address and the key says who it is: only the project ID has to be typed
      return { dialect: st.kind, host: c.host.trim() || "firestore.googleapis.com", port: c.port ? Number(c.port) : d.default_port, database: c.database.trim(), user: c.user.trim() || "service-account", options };
    }
    return { dialect: st.kind, host: c.host.trim(), port: c.port ? Number(c.port) : d.default_port, database: c.database.trim(), user: c.user.trim(), options };
  }

  // the encryption settings differ per database; presets (Azure) switch the right ones on
  function tlsRows(st, prop) {
    const o = st.conn.opts || {};
    const box = (key, def) => '<input type="checkbox" data-opt="' + key + '"' + ((o[key] === undefined ? def : o[key]) ? " checked" : "") + ">";
    const file = (key, ph) => '<input type="text" data-opt="' + key + '" value="' + esc(o[key] || "") + '" placeholder="' + esc(ph) + '" autocomplete="off" spellcheck="false">';
    if (st.kind === "mssql") {
      return prop("encrypt", "Encrypt connection", box("encrypt", false), "Turn on for Azure SQL (always required there) or any server that forces encryption.") +
        prop("cafile", "CA certificate file", file("cafile", "(public CA bundle)"), "A PEM file on the server that holds the CA which signed the database certificate. Leave empty for Azure.") +
        prop("validate_host", "Verify host name", box("validate_host", true), "Check that the certificate names the server you typed. Connections by IP address need this off.");
    }
    if (st.kind === "postgresql") {
      return prop("sslmode", "SSL mode", '<select data-opt="sslmode">' + options([["", "prefer (default)"], ["disable", "disable"], ["require", "require (Azure)"], ["verify-ca", "verify-ca"], ["verify-full", "verify-full"]], o.sslmode || "") + "</select>", "require encrypts; verify-full also checks the certificate and host name.") +
        prop("sslrootcert", "Root certificate file", file("sslrootcert", "(optional)"), "PEM file on the server with the CA, needed by verify-ca / verify-full.");
    }
    if (st.kind === "mysql") {
      return prop("ssl", "Use TLS", box("ssl", false), "Encrypt the connection (Azure always requires it). Left unticked the driver still uses TLS when the server offers it.") +
        prop("ssl_ca", "CA certificate file", file("ssl_ca", "(public CA bundle)"), "PEM file on the server with the CA that signed the database certificate.") +
        prop("ssl_verify", "Verify certificate", box("ssl_verify", true), "Check the certificate and host name. Switch off only for a throw-away test server.");
    }
    if (st.kind === "firestore") {
      return prop("database_id", "Firestore database", file("database_id", "(default)"), "Only if the project has more than one Firestore database. Leave empty for the default one.");
    }
    if (st.kind === "mongodb") {
      return prop("auth_source", "Authentication database", file("auth_source", "(defaults to the database above)"), "The database the user is defined in (often admin). Leave empty to use the database above.") +
        prop("tls", "Use TLS", box("tls", false), "Encrypt the connection (MongoDB Atlas and most managed services require it).") +
        prop("tls_ca_file", "CA certificate file", file("tls_ca_file", "(public CA bundle)"), "PEM file on the server with the CA that signed the database certificate.");
    }
    return "";
  }

  // choosing a cloud preset fills in the encryption the provider demands
  function applyPreset(st, value) {
    if (value && value.startsWith("preset:")) {
      const pr = (st.presets || []).find((x) => "preset:" + x.id === value);
      if (pr) { st.kind = pr.dialect; st.preset = pr.id; st.conn.port = String(pr.port); st.conn.opts = Object.assign({}, pr.options); return; }
    }
    st.kind = value; st.preset = null; st.conn.opts = {}; st.conn.port = "";   // a plain choice drops everything a preset filled in, port included
  }
  const STORAGE_KINDS = ["azure_blob", "s3", "sftp"];
  const isStorage = (st) => STORAGE_KINDS.includes(st.kind);
  const storageOptions = (st) => st.dialects.filter((d) => d.family === "storage").map((d) => '<option value="' + esc(d.dialect) + '"' + (st.kind === d.dialect ? " selected" : "") + ">" + esc(d.label) + "</option>").join("");
  const SOURCE_ONLY = ["mongodb"];          // MongoDB is read, never written (Firestore can receive a delivery as well)
  const kindOptions = (st, selected, forTarget) => st.dialects.filter((d) => d.family !== "storage" && !(forTarget && SOURCE_ONLY.includes(d.dialect))).map((d) => '<option value="' + esc(d.dialect) + '"' + (!st.preset && selected === d.dialect ? " selected" : "") + ">" + esc(d.label) + "</option>").join("") +
    ((st.presets || []).length ? "</optgroup><optgroup label=\"Cloud databases\">" + st.presets.map((p) => '<option value="preset:' + esc(p.id) + '"' + (st.preset === p.id ? " selected" : "") + ">" + esc(p.label) + "</option>").join("") : "");

  const dbConnectionStep = {
    label: "Set connection", desc: "Specify the connection parameters for the source database. Test must succeed before you continue.",
    help: "Use a read-only account: the server never writes to a source, but a read-only account makes that a guarantee. The password is held in the server's memory only (or read from the environment variable you name) and is never stored or shown again.",
    render(el, st) {
      const d = st.dialects.find((x) => x.dialect === st.kind) || { default_port: "", database_label: "Database" }, fs = st.kind === "firestore";
      const prop = (key, label, control, help) => '<tr data-key="' + key + '" data-help="' + esc(label + "|" + help) + '"><td>' + esc(label) + "</td><td>" + control + "</td></tr>";
      const text = (id, value, ph, type) => '<input type="' + (type || "text") + '" id="' + id + '" value="' + esc(value) + '" placeholder="' + esc(ph || "") + '" autocomplete="off" spellcheck="false">';
      el.innerHTML = '<div class="pg-wrap"><table class="pg"><tbody><tr class="grp"><td colspan="2">&#9662; Required</td></tr>' +
        (fs ? (
        prop("database", "Project ID", text("c-database", st.conn.database, "my-app-1234"), "Firebase console > Project settings > Project ID. It is filled in for you when you load the key file.") +
        prop("keyfile", "Key file", '<button type="button" class="wz-btn" id="c-keyfile">Load key file...</button><input type="file" id="c-keyinput" accept=".json,application/json" hidden>', "Google Cloud console > IAM & Admin > Service accounts > Keys > Add key > JSON. The file is read in your browser and sent to this server's memory only. Give the service account only the Cloud Datastore Viewer role.") +
        prop("password", "Service-account key", '<textarea id="c-password" class="mask" rows="3" spellcheck="false" autocomplete="off" placeholder="Paste the whole key file here, or press Load key file">' + esc(st.conn.password) + "</textarea>", "The content of the key file. Kept in server memory only; never stored or displayed again.") +
          "") : (
        prop("host", "Server", text("c-host", st.conn.host, "db.company.local"), "Host name or IP address of the database server.") +
        prop("port", "Port", text("c-port", st.conn.port, String(d.default_port), "number"), "Leave empty for the default (" + d.default_port + ").") +
        prop("database", d.database_label, text("c-database", st.conn.database, st.kind === "oracle" ? "ORADB1" : ""), st.kind === "oracle" ? "The Oracle service name." : "The database to connect to.") +
        prop("user", "User ID", text("c-user", st.conn.user, "read-only account"), "A read-only account is strongly recommended.") +
        prop("password", "Password", text("c-password", st.conn.password, "", "password").replace('autocomplete="off"', 'autocomplete="new-password"'), "Kept in server memory only; never stored or displayed."))) +
        '<tr class="grp"><td colspan="2">&#9662; Optional</td></tr>' +
        (fs ? prop("host", "Server (emulator only)", text("c-host", st.conn.host, "firestore.googleapis.com"), "Leave empty for Google. Only the Firestore emulator on this computer (localhost) is accepted instead.") +
          prop("port", "Port (emulator only)", text("c-port", st.conn.port, "443", "number"), "Leave empty for Google.") +
          prop("user", "Service account", text("c-user", st.conn.user, "filled in from the key file"), "The service account's e-mail. Informational: the key itself says who it is.") : "") +
        prop("password_env", fs ? "Key variable" : "Password variable", text("c-env", st.conn.password_env, "e.g. LEGACY_DB_PASSWORD"), "Instead of typing a password: the name of an environment variable on the server that holds it. It survives a server restart.") +
        prop("timeout", "Connection timeout (s)", text("c-timeout", st.conn.timeout, "10", "number"), "Seconds to wait for the server before giving up.") +
        tlsRows(st, prop) +
        "</tbody></table></div>" +
        '<div class="pg-help" id="pg-help"><strong>Server</strong>Host name or IP address of the database server.</div>' +
        '<div class="wz-actionrow"><span class="wz-status' + (st.dbtest ? (st.dbtest.ok ? " ok" : " bad") : "") + '" id="c-status">' +
        esc(st.dbtest ? (st.dbtest.ok ? "Test succeeded: " + (st.dbtest.server_version || "") + " - " + st.dbtest.schemas + " schema(s) visible (" + st.dbtest.latency_ms + " ms)" : st.dbtest.detail) : "Press Test to connect.") +
        '</span><button type="button" class="wz-btn" id="c-test">Test</button></div>';
      const map = [["#c-host", "host"], ["#c-port", "port"], ["#c-database", "database"], ["#c-user", "user"], ["#c-password", "password"], ["#c-env", "password_env"], ["#c-timeout", "timeout"]];
      st.conn.opts = st.conn.opts || {};
      el.querySelectorAll("[data-opt]").forEach((c) => c.addEventListener(c.tagName === "SELECT" || c.type === "checkbox" ? "change" : "input", () => {
        st.conn.opts[c.dataset.opt] = c.type === "checkbox" ? c.checked : c.value; st.dbtest = null; st.connSaved = false; refreshNav();
      }));
      map.forEach(([id, key]) => { const c = $(id); if (c) c.addEventListener(c.tagName === "SELECT" ? "change" : "input", () => { st.conn[key] = c.value; st.dbtest = null; st.connSaved = false; refreshNav(); }); });
      el.querySelectorAll("tr[data-key]").forEach((tr) => tr.addEventListener("focusin", () => {
        el.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel");
        const [title, textHelp] = tr.dataset.help.split("|"); $("#pg-help").innerHTML = "<strong>" + esc(title) + "</strong>" + esc(textHelp);
      }));
      const pick = $("#c-keyfile");
      if (pick) {
        const input = $("#c-keyinput");
        pick.addEventListener("click", () => input.click());
        input.addEventListener("change", async () => {
          const file = input.files && input.files[0];
          if (!file) return;
          const status = $("#c-status");
          try {
            const text = await file.text(), key = JSON.parse(text);
            if (key.type !== "service_account" || !key.private_key) throw new Error("That file is not a service-account key (in the Google Cloud console create a key of type JSON for a service account)");
            st.conn.password = text; st.conn.database = key.project_id || st.conn.database; st.conn.user = key.client_email || st.conn.user;
            st.dbtest = null; st.connSaved = false; renderStep();
          } catch (e) { status.className = "wz-status bad"; status.textContent = e instanceof SyntaxError ? "That file is not valid JSON" : e.message; }
        });
      }
      $("#c-test").addEventListener("click", async () => {
        const status = $("#c-status"); status.className = "wz-status"; status.textContent = "Connecting...";
        $("#c-test").disabled = true;
        try {
          const body = connBody(st);
          const emulator = st.kind === "firestore" && ["localhost", "127.0.0.1"].includes(body.host.toLowerCase());
          if (!st.conn.password && !st.conn.password_env && !emulator) throw new Error(st.kind === "firestore" ? "Load the service-account key file (or paste its content), or name a server environment variable that holds it" : "Enter a password, or the name of a server environment variable that holds it");
          if (st.kind === "firestore" && !st.conn.database.trim()) throw new Error("Enter the Firebase project ID");
          st.dbtest = await api("POST", "/api/connections/test", Object.assign({}, body, { password: st.conn.password || undefined, password_env: st.conn.password_env.trim() || undefined }));
          if (st.dbtest.ok) {
            const existing = (await api("GET", "/api/connections")).some((c) => c.name === st.name.trim());
            await api("POST", "/api/connections", Object.assign({ name: st.name.trim(), role: st.role || "source", password: st.conn.password || undefined, password_env: st.conn.password_env.trim() || undefined }, body));
            if (!existing) st.createdNames.push(st.name.trim());     // removed again if the wizard is cancelled
            st.connSaved = true; st.schemasList = null; st.tablesBySchema = {}; st.selected = new Map();
          }
        } catch (e) { st.dbtest = { ok: false, detail: e.message }; }
        renderStep();
      });
    },
    gate: (st) => (st.dbtest && st.dbtest.ok && st.connSaved ? null : "Press Test and get a successful result first"),
  };

  const STORAGE_FIELDS = {
    azure_blob: { host: ["Endpoint host", "<account>.blob.core.windows.net", "The storage account's blob endpoint (Data Lake Gen2 accounts work through it). For the Azurite emulator: localhost."],
      user: ["Account name", "", "The storage account name."], password: ["Account key / SAS token", "", "Kept in server memory only; never stored or shown again."],
      database: ["Default container", "(optional)", "Leave empty to browse every container the account can list."] },
    s3: { host: ["Endpoint", "s3.amazonaws.com", "s3.amazonaws.com for AWS, or the host of any S3-compatible service."], user: ["Access key ID", "", "The access key of a read-only user."],
      password: ["Secret access key", "", "Kept in server memory only; never stored or shown again."], database: ["Default bucket", "(optional)", "Leave empty to browse every bucket this user may list."] },
    sftp: { host: ["Server", "sftp.company.local", "Host name or IP address of the SFTP server."], user: ["User ID", "", "A read-only account."], password: ["Password", "", "Kept in server memory only; never stored or shown again."],
      database: ["Base directory", "/exports", "The folder the browser starts in; nothing outside it can be read."] },
  };

  const storageConnectionStep = {
    label: "Set connection", desc: "Specify how to reach the storage account. Test must succeed before you continue.",
    help: "Nothing on the remote side is ever changed: files are only listed and downloaded. For SFTP the server's identity (its host key fingerprint) has to be confirmed once, so a look-alike server cannot collect your password.",
    render(el, st) {
      const f = STORAGE_FIELDS[st.kind], d = st.dialects.find((x) => x.dialect === st.kind) || { default_port: "" }, o = st.conn.opts = st.conn.opts || {};
      const prop = (key, label, control, help) => '<tr data-key="' + key + '" data-help="' + esc(label + "|" + help) + '"><td>' + esc(label) + "</td><td>" + control + "</td></tr>";
      const text = (id, value, ph, type) => '<input type="' + (type || "text") + '" id="' + id + '" value="' + esc(value) + '" placeholder="' + esc(ph || "") + '" autocomplete="off" spellcheck="false">';
      const box = (key, def) => '<input type="checkbox" data-opt="' + key + '"' + ((o[key] === undefined ? def : o[key]) ? " checked" : "") + ">";
      el.innerHTML = '<div class="pg-wrap"><table class="pg"><tbody><tr class="grp"><td colspan="2">&#9662; Required</td></tr>' +
        prop("host", f.host[0], text("c-host", st.conn.host, f.host[1]), f.host[2]) + prop("user", f.user[0], text("c-user", st.conn.user, f.user[1]), f.user[2]) +
        prop("password", f.password[0], text("c-password", st.conn.password, "", "password").replace('autocomplete="off"', 'autocomplete="new-password"'), f.password[2]) +
        '<tr class="grp"><td colspan="2">&#9662; Optional</td></tr>' +
        prop("database", f.database[0], text("c-database", st.conn.database, f.database[1]), f.database[2]) +
        prop("port", "Port", text("c-port", st.conn.port, String(d.default_port), "number"), "Leave empty for the default (" + d.default_port + ").") +
        (st.kind === "azure_blob" ? prop("auth", "Credential type", '<select data-opt="auth">' + options([["key", "account key"], ["sas", "SAS token"]], o.auth || "key") + "</select>", "An account key, or a shared access signature token.") : "") +
        (st.kind === "s3" ? prop("region", "Region", text("c-region", o.region || "", "us-east-1").replace('id="c-region"', 'data-opt="region"'), "The bucket's region (AWS). Leave as default for other services.") : "") +
        (st.kind !== "sftp" ? prop("https", "Use HTTPS", box("https", true), "Switch off only for a local test emulator.") +
          prop("path_style", "Path-style URL", box("path_style", false), "On for emulators and most S3-compatible services (Azurite, CloudServer, MinIO).") : "") +
        (st.kind === "sftp" ? prop("host_key_sha256", "Host key fingerprint", text("c-hostkey", o.host_key_sha256 || "", "SHA256:... (confirmed on first test)"), "The server's key fingerprint. Leave empty and press Test: the server shows its fingerprint and you decide whether to trust it.") : "") +
        prop("timeout", "Connection timeout (s)", text("c-timeout", st.conn.timeout, "10", "number"), "Seconds to wait for the server before giving up.") +
        "</tbody></table></div>" +
        '<div class="pg-help" id="pg-help"><strong>' + esc(f.host[0]) + "</strong>" + esc(f.host[2]) + "</div>" +
        (st.dbtest && st.dbtest.host_key ? '<div class="banner amber">The server presented the key <code>' + esc(st.dbtest.host_key) + '</code>. Check it against the fingerprint the server\'s owner gave you; ' +
          'trust it only if it is identical. <button type="button" class="wz-btn" id="c-trust">Trust this key</button></div>' : "") +
        '<div class="wz-actionrow"><span class="wz-status' + (st.dbtest ? (st.dbtest.ok ? " ok" : " bad") : "") + '" id="c-status">' +
        esc(st.dbtest ? (st.dbtest.ok ? "Test succeeded: " + (st.dbtest.server_version || "") + " - " + st.dbtest.schemas + " container(s) visible (" + st.dbtest.latency_ms + " ms)" : st.dbtest.detail) : "Press Test to connect.") +
        '</span><button type="button" class="wz-btn" id="c-test">Test</button></div>';
      const map = [["#c-host", "host"], ["#c-port", "port"], ["#c-database", "database"], ["#c-user", "user"], ["#c-password", "password"], ["#c-timeout", "timeout"]];
      map.forEach(([id, key]) => { const c = $(id); if (c) c.addEventListener("input", () => { st.conn[key] = c.value; st.dbtest = null; st.connSaved = false; refreshNav(); }); });
      el.querySelectorAll("[data-opt]").forEach((c) => c.addEventListener(c.tagName === "SELECT" || c.type === "checkbox" ? "change" : "input", () => {
        o[c.dataset.opt] = c.type === "checkbox" ? c.checked : c.value; st.dbtest = null; st.connSaved = false; refreshNav();
      }));
      const hk = $("#c-hostkey"); if (hk) hk.addEventListener("input", () => { o.host_key_sha256 = hk.value.trim(); st.dbtest = null; st.connSaved = false; refreshNav(); });
      el.querySelectorAll("tr[data-key]").forEach((tr) => tr.addEventListener("focusin", () => {
        el.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel");
        const [title, textHelp] = tr.dataset.help.split("|"); $("#pg-help").innerHTML = "<strong>" + esc(title) + "</strong>" + esc(textHelp);
      }));
      const trust = $("#c-trust");
      if (trust) trust.addEventListener("click", () => { o.host_key_sha256 = st.dbtest.host_key; st.dbtest = null; renderStep(); setTimeout(() => $("#c-test").click(), 0); });
      $("#c-test").addEventListener("click", async () => {
        const status = $("#c-status"); status.className = "wz-status"; status.textContent = "Connecting...";
        $("#c-test").disabled = true;
        try {
          const body = connBody(st);
          if (!st.conn.password) throw new Error("Enter the " + f.password[0].toLowerCase());
          st.dbtest = await api("POST", "/api/connections/test", Object.assign({}, body, { password: st.conn.password }));
          if (st.dbtest.ok) {
            const existing = (await api("GET", "/api/connections")).some((c) => c.name === st.name.trim());
            await api("POST", "/api/connections", Object.assign({ name: st.name.trim(), role: "source", password: st.conn.password }, body));
            if (!existing) st.createdNames.push(st.name.trim());
            st.connSaved = true; st.selectedFiles = new Map(); st.container = ""; st.prefix = "";
          }
        } catch (e) { st.dbtest = { ok: false, detail: e.message }; }
        renderStep();
      });
    },
    gate: (st) => (st.dbtest && st.dbtest.ok && st.connSaved ? null : "Press Test and get a successful result first"),
  };

  const bytesText = (n) => (n === null || n === undefined ? "" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const storageFilesStep = {
    label: "Select tables", desc: "Browse to the extract files and tick the ones to bring in. Each file becomes a table with its format detected automatically.",
    help: "Folders open when clicked. Files are read exactly like uploaded ones: delimited text, Excel, JSON, fixed-width, Parquet, optionally .gz or .zip. A file that cannot be read is reported and the others still import.",
    render(el, st) {
      const name = st.name.trim();
      st.selectedFiles = st.selectedFiles || new Map();
      el.innerHTML = '<div class="wz-filterbar"><label>' + esc(st.kind === "s3" ? "Bucket" : st.kind === "sftp" ? "Directory" : "Container") + ': <select id="f-container"><option>Loading...</option></select></label>' +
        '<span class="wz-status" id="f-count"></span></div><div class="wz-crumbs" id="f-crumbs"></div><div class="wz-tree" id="f-list"><p class="muted">Loading...</p></div>';
      const list = $("#f-list"), count = () => { $("#f-count").textContent = st.selectedFiles.size + " selected"; refreshNav(); };
      const crumbs = () => {
        const parts = (st.prefix || "").split("/").filter(Boolean);
        $("#f-crumbs").innerHTML = '<a href="#" data-go="">/</a>' + parts.map((x, i) => ' <span class="muted">&rsaquo;</span> <a href="#" data-go="' + esc(parts.slice(0, i + 1).join("/") + "/") + '">' + esc(x) + "</a>").join("");
      };
      const load = async () => {
        crumbs();
        list.innerHTML = '<p class="muted">Loading...</p>';
        try {
          const rows = await api("GET", "/api/connections/" + encodeURIComponent(name) + "/files?container=" + encodeURIComponent(st.container) + "&prefix=" + encodeURIComponent(st.prefix || ""));
          list.innerHTML = rows.map((r) => r.is_dir
            ? '<div class="wz-tbl"><svg class="ic"><use href="#i-folder"/></svg> <a href="#" data-go="' + esc(r.key) + '">' + esc(r.name) + "</a></div>"
            : '<div class="wz-tbl"><input type="checkbox" data-key="' + esc(r.key) + '" data-size="' + esc(r.size === null ? "" : r.size) + '"' + (st.selectedFiles.has(st.container + "|" + r.key) ? " checked" : "") + '> <span>' + esc(r.name) +
              '</span> <span class="muted">' + esc(bytesText(r.size)) + (r.modified ? ", " + esc(r.modified.replace("T", " ")) : "") + "</span></div>").join("") || '<p class="muted">This folder is empty.</p>';
        } catch (e) { list.innerHTML = '<div class="banner red">' + esc(e.message) + "</div>"; }
        count();
      };
      list.addEventListener("change", (e) => {
        const c = e.target.closest("input[data-key]"); if (!c) return;
        const k = st.container + "|" + c.dataset.key;
        if (c.checked) st.selectedFiles.set(k, { container: st.container, key: c.dataset.key, size: c.dataset.size === "" ? null : Number(c.dataset.size) }); else st.selectedFiles.delete(k);
        count();
      });
      el.addEventListener("click", (e) => { const a = e.target.closest("a[data-go]"); if (!a) return; e.preventDefault(); st.prefix = a.dataset.go; load(); });
      api("GET", "/api/connections/" + encodeURIComponent(name) + "/containers").then((cs) => {
        if (!cs.includes(st.container)) { st.container = cs[0] || ""; st.prefix = ""; }
        $("#f-container").innerHTML = cs.map((c) => "<option" + (c === st.container ? " selected" : "") + ">" + esc(c) + "</option>").join("") || "<option value=''>(nothing visible)</option>";
        if (st.container) load(); else list.innerHTML = '<p class="muted">This account cannot see any container.</p>';
      }).catch((e) => { list.innerHTML = '<div class="banner red">' + esc(e.message) + "</div>"; });
      $("#f-container").addEventListener("change", (e) => { st.container = e.target.value; st.prefix = ""; load(); });
    },
    gate: (st) => (st.selectedFiles && st.selectedFiles.size ? null : "Tick at least one file"),
  };

  const storageSummaryStep = {
    label: "Summary", desc: "Check that the source contains the correct values.",
    render(el, st) {
      const d = st.dialects.find((x) => x.dialect === st.kind) || {}, items = [...st.selectedFiles.values()];
      el.innerHTML = '<fieldset><legend>Source summary</legend>' + summary([["Source name", st.name.trim()], ["Provider", d.label || st.kind], ["Server", st.conn.host.trim() + ":" + (st.conn.port || d.default_port)],
        ["Account / user", st.conn.user.trim() + " (files are only read)"], ["Files", items.length + " selected"]]) + "</fieldset>" +
        '<fieldset><legend>Files</legend><div class="wz-cols"><table><thead><tr><th>File</th><th>Size</th></tr></thead><tbody>' +
        items.map((f) => "<tr><td>" + esc(f.container + " / " + f.key) + "</td><td>" + esc(bytesText(f.size)) + "</td></tr>").join("") + "</tbody></table></div></fieldset>" +
        '<fieldset><legend>Click Finish to perform the following actions</legend>' + summary([["", "Download each file and land it as a snapshot (format, encoding and delimiter detected)"], ["", "Register one table per file"],
          st.profile ? ["", "Profile each table"] : null, st.studio ? ["", "Open the Mapping Studio for the first table"] : null, st.target ? ["", "Launch the Add Target Connection Wizard"] : null]) + "</fieldset>";
    },
  };

  async function finishStorage(st) {
    const items = [...st.selectedFiles.values()], name = st.name.trim();
    st.results = st.results || new Map();
    const body = $("#wz-body");
    const paint = () => { body.innerHTML = '<fieldset><legend>Importing</legend><div class="wz-cols"><table><tbody>' + items.map((f) => {
      const r = st.results.get(f.container + "|" + f.key);
      const state = !r ? "waiting" : r.pending ? "downloading..." : r.ok ? "&#10003; " + esc(r.table + ": " + num(r.rows) + " rows, " + r.format) : "&#10007; " + esc(r.msg);
      return "<tr><td>" + esc(f.key) + "</td><td>" + state + "</td></tr>"; }).join("") + "</tbody></table></div></fieldset>"; };
    let failed = 0, first = null;
    for (const f of items) {
      const k = f.container + "|" + f.key;
      if (st.results.get(k) && st.results.get(k).ok) { first = first || st.results.get(k).reg; continue; }
      st.results.set(k, { pending: true }); paint();
      try {
        const r = await api("POST", "/api/sources/import-file", { connection: name, container: f.container, key: f.key });
        let note = "";
        if (st.profile) { try { await api("POST", "/api/profile/" + r.table_id); } catch (e) { note = " (profiling failed)"; } }
        st.results.set(k, { ok: true, rows: r.rows_landed, format: r.format + note, table: r.table_name, reg: r });
        first = first || r;
      } catch (e) { failed += 1; st.results.set(k, { ok: false, msg: e.message }); }
      paint();
    }
    if (failed) throw new Error(failed + " of " + items.length + " file(s) failed. Fix the cause and press Finish to retry only those.");
    st.conn.password = ""; st.finished = true;
    for (const n of st.createdNames) { if (n !== name) { try { await api("DELETE", "/api/connections/" + encodeURIComponent(n)); } catch (e) { /* already gone */ } } }
    App.toast("Imported " + items.length + " file(s) from " + name);
    if (first) {
      await window.Shell.reveal(first.table_id, first.system_name);
      if (st.profile) window.Shell.openTab("/ui/profiler/" + first.table_id);
      if (st.studio) window.Shell.openTab("/ui/studio/" + first.table_id);
    }
    if (st.target) setTimeout(openTarget, 0);
  }

  const dbTablesStep = {
    label: "Select tables", desc: "Expand the schemas and tick the tables (or views) to bring in. Each becomes a snapshot you can profile and map.",
    help: "The primary key comes from the database. Row counts are checked against the source: an extract is rejected unless exactly the rows the database reports were read. Views have no key.",
    render(el, st) {
      const name = st.name.trim();
      el.innerHTML = '<div class="wz-filterbar"><label>Filter: <input type="text" id="d-filter" value="' + esc(st.filter) + '" placeholder="table name contains..."></label>' +
        '<label class="inline"><input type="checkbox" id="d-hide"' + (st.hideSystem ? " checked" : "") + "> Hide system schemas</label>" +
        '<span class="wz-status" id="d-count"></span></div><div class="wz-tree" id="d-tree"><p class="muted">Loading schemas...</p></div>' +
        '<div class="pg-help" id="d-cols"><strong>Columns</strong>Click a table name to see its columns.</div>' +
        '<div class="wz-checks"><label><input type="checkbox" id="d-control"' + (st.control ? " checked" : "") + "> Verify row counts against the source (recommended)</label></div>";
      const tree = $("#d-tree"), count = () => { $("#d-count").textContent = st.selected.size + " selected"; refreshNav(); };
      const tableRows = (schema) => (st.tablesBySchema[schema] || []).map((t) => {
        const key = schema + "." + t.name, done = st.results && st.results.get(key) && st.results.get(key).ok;
        return '<div class="wz-tbl"><input type="checkbox" data-schema="' + esc(schema) + '" data-table="' + esc(t.name) + '"' + (st.selected.has(key) ? " checked" : "") + (done ? " disabled" : "") + ">" +
          '<a href="#" class="tbl-name" data-schema="' + esc(schema) + '" data-table="' + esc(t.name) + '">' + esc(t.name) + '</a> <span class="muted">' + (t.kind === "VIEW" ? "view" : "table") +
          (t.row_estimate !== null && t.row_estimate !== undefined ? ", ~" + num(t.row_estimate) + " rows" : "") + (done ? " - imported" : "") + "</span></div>";
      }).join("") || '<p class="muted">No tables match.</p>';
      const draw = () => {
        tree.innerHTML = (st.schemasList || []).map((sc) => {
          const open = st.openSchemas.has(sc);
          return '<div class="wz-schema" data-schema="' + esc(sc) + '"><div class="hdr"><button type="button" class="twist-btn" data-toggle="' + esc(sc) + '">' + (open ? "&minus;" : "+") + "</button> " +
            '<svg class="ic"><use href="#i-folder"/></svg> ' + esc(sc) + '</div><div class="tbls' + (open ? "" : " hidden") + '">' + (open ? tableRows(sc) : "") + "</div></div>";
        }).join("") || '<p class="muted">This account cannot see any schema.</p>';
        count();
        if (typeof tablesDrawn === "function") tablesDrawn();       // the guide can only suggest a table once the list is on the screen
      };
      const loadTables = async (schema) => {
        st.tablesBySchema[schema] = await api("GET", "/api/connections/" + enc(name) + "/tables?schema_name=" + enc(schema) + "&filter=" + enc(st.filter));
      };
      const load = async () => {
        try {
          st.schemasList = await api("GET", "/api/connections/" + enc(name) + "/schemas?include_system=" + (!st.hideSystem));
          if (!st.openSchemas.size && st.schemasList.length === 1) st.openSchemas.add(st.schemasList[0]);
          await Promise.all([...st.openSchemas].filter((x) => st.schemasList.includes(x)).map(loadTables));
          draw();
        } catch (e) { tree.innerHTML = '<div class="banner red">' + esc(e.message) + "</div>"; }
      };
      tree.addEventListener("click", async (e) => {
        const tg = e.target.closest("[data-toggle]");
        if (tg) {
          const sc = tg.dataset.toggle;
          if (st.openSchemas.has(sc)) st.openSchemas.delete(sc); else { st.openSchemas.add(sc); if (!st.tablesBySchema[sc]) { try { await loadTables(sc); } catch (err) { showError(err.message); } } }
          draw(); return;
        }
        const nm = e.target.closest(".tbl-name");
        if (nm) {
          e.preventDefault();
          const box = $("#d-cols"); box.innerHTML = "<strong>" + esc(nm.dataset.schema + "." + nm.dataset.table) + "</strong>Reading columns...";
          try {
            const cols = await api("GET", "/api/connections/" + enc(name) + "/columns?schema_name=" + enc(nm.dataset.schema) + "&table=" + enc(nm.dataset.table));
            box.innerHTML = "<strong>" + esc(nm.dataset.schema + "." + nm.dataset.table) + " - " + cols.length + " columns</strong>" + cols.map((c) =>
              esc(c.name) + " <span class='muted'>" + esc(c.data_type) + (c.is_primary_key ? ", primary key" : "") + (c.nullable ? "" : ", not null") + "</span>").join(" &middot; ");
          } catch (err) { box.innerHTML = "<strong>" + esc(nm.dataset.table) + "</strong>" + esc(err.message); }
        }
      });
      tree.addEventListener("change", (e) => {
        const c = e.target.closest("input[data-table]"); if (!c) return;
        const key = c.dataset.schema + "." + c.dataset.table;
        const t = (st.tablesBySchema[c.dataset.schema] || []).find((x) => x.name === c.dataset.table);
        if (c.checked) st.selected.set(key, { schema: c.dataset.schema, table: c.dataset.table, kind: t ? t.kind : "TABLE", rows: t ? t.row_estimate : null }); else st.selected.delete(key);
        count();
      });
      let timer = null;
      $("#d-filter").addEventListener("input", (e) => { st.filter = e.target.value; clearTimeout(timer); timer = setTimeout(() => load(), 350); });
      $("#d-hide").addEventListener("change", (e) => { st.hideSystem = e.target.checked; load(); });
      $("#d-control").addEventListener("change", (e) => { st.control = e.target.checked; });
      load();
    },
    gate: (st) => (st.selected.size ? null : "Tick at least one table"),
  };

  const dbSummaryStep = {
    label: "Summary", desc: "Check that the source contains the correct values.",
    render(el, st) {
      const d = st.dialects.find((x) => x.dialect === st.kind) || {};
      const items = [...st.selected.values()];
      el.innerHTML = '<fieldset><legend>Source summary</legend>' + summary([
        ["Source name", st.name.trim()], ["Provider", (d.label || st.kind)], ["Server", st.conn.host.trim() + ":" + (st.conn.port || d.default_port)], [d.database_label || "Database", st.conn.database.trim() || "(default)"],
        ["User", st.conn.user.trim() + " (read-only session)"], ["Tables", items.length + " selected"], ["Row-count check", st.control ? "on - rejected unless every source row arrives" : "off"]]) + "</fieldset>" +
        '<fieldset><legend>Tables</legend><div class="wz-cols"><table><thead><tr><th>Table</th><th>Type</th><th>Est. rows</th></tr></thead><tbody>' +
        items.map((t) => "<tr><td>" + esc(t.schema + "." + t.table) + "</td><td>" + (t.kind === "VIEW" ? "view" : "table") + "</td><td>" + (t.rows === null || t.rows === undefined ? "-" : num(t.rows)) + "</td></tr>").join("") + "</tbody></table></div></fieldset>" +
        '<fieldset><legend>Click Finish to perform the following actions</legend>' + summary([["", "Extract each table (read-only) and land it as a snapshot"], ["", "Register the tables with the primary keys defined in the database"],
          st.profile ? ["", "Profile each table"] : null, st.studio ? ["", "Open the Mapping Studio for the first table"] : null, st.target ? ["", "Launch the Add Target Connection Wizard"] : null]) + "</fieldset>";
    },
  };

  const dbSteps = [null, dbConnectionStep, dbTablesStep, null, dbSummaryStep];
  const storageSteps = [null, storageConnectionStep, storageFilesStep, null, storageSummaryStep];
  const sourceSteps = fileSteps.map((fs, i) => {
    const isDb = (st) => st.kind !== "file";
    const pick = (st) => (i !== 0 && i !== 3 && isDb(st) ? (isStorage(st) ? storageSteps[i] : dbSteps[i]) : fs);
    return {
      get label() { return i === 2 ? "Select table(s)" : fs.label; }, desc: fs.desc, help: fs.help,
      render: (el, st, w) => { const step = pick(st); $("#wz-desc").innerHTML = esc(step.desc || fs.desc) + (w.showHelp && (step.help || fs.help) ? '<span class="more">' + esc(step.help || fs.help) + "</span>" : ""); step.render(el, st, w); },
      gate: (st) => (pick(st).gate ? pick(st).gate(st) : null),
      check: (st) => (pick(st).check ? pick(st).check(st) : null),
    };
  });

  async function finishDb(st) {
    const items = [...st.selected.values()], name = st.name.trim();
    st.results = st.results || new Map();
    const body = $("#wz-body");
    const paint = () => { body.innerHTML = '<fieldset><legend>Importing</legend><div class="wz-cols"><table><tbody>' + items.map((t) => {
      const r = st.results.get(t.schema + "." + t.table);
      const state = !r ? "waiting" : r.pending ? "reading..." : r.ok ? "&#10003; " + esc(num(r.rows) + " rows, " + r.control) : "&#10007; " + esc(r.msg);
      return "<tr><td>" + esc(t.schema + "." + t.table) + "</td><td>" + state + "</td></tr>"; }).join("") + "</tbody></table></div></fieldset>"; };
    let failed = 0, first = null;
    for (const t of items) {
      const key = t.schema + "." + t.table;
      if (st.results.get(key) && st.results.get(key).ok) { first = first || st.results.get(key).table; continue; }
      st.results.set(key, { pending: true }); paint();
      try {
        const r = await api("POST", "/api/sources/import-db", { connection: name, schema_name: t.schema, table: t.table, control_total: st.control });
        let note = "";
        if (st.profile) { try { await api("POST", "/api/profile/" + r.table_id); } catch (e) { note = " (profiling failed)"; } }
        st.results.set(key, { ok: true, rows: r.rows_landed, control: (r.control_status === "MATCH" ? "count verified" : "no count check") + note, table: r });
        first = first || r;
      } catch (e) { failed += 1; st.results.set(key, { ok: false, msg: e.message }); }
      paint();
    }
    if (failed) throw new Error(failed + " of " + items.length + " table(s) failed. Fix the cause and press Finish to retry only those.");
    st.conn.password = "";                                  // the browser keeps no secret once the wizard is done
    st.finished = true;
    for (const n of st.createdNames) { if (n !== name) { try { await api("DELETE", "/api/connections/" + encodeURIComponent(n)); } catch (e) { /* already gone */ } } }
    App.toast("Imported " + items.length + " table(s) from " + name);
    if (first) {
      await window.Shell.reveal(first.table_id, first.system_name);
      if (st.profile) window.Shell.openTab("/ui/profiler/" + first.table_id);
      if (st.studio) window.Shell.openTab("/ui/studio/" + first.table_id);
    }
    if (st.target) setTimeout(openTarget, 0);
  }

  async function finishSource(st) {
    const form = new FormData();
    form.append("file", st.file); form.append("system_name", st.name.trim()); form.append("system_type", "FLAT_FILE"); form.append("table_name", st.table.trim());
    if (st.business.trim()) form.append("business_name", st.business.trim());
    if (st.keys.size) form.append("primary_key_columns", [...st.keys].join(","));
    form.append("options", JSON.stringify(apiOptions(st)));
    if (st.expected !== "") form.append("expected_rows", st.expected);
    const r = await api("POST", "/api/sources/register", form, true);
    let note = "";
    if (st.profile) { try { await api("POST", "/api/profile/" + r.table_id); } catch (e) { note = " (profiling failed: " + e.message + ")"; } }
    App.toast("Source " + r.system_name + " / " + r.table_name + " registered: " + num(r.rows_landed) + " rows, " + r.columns_registered + " columns" + note);
    await window.Shell.reveal(r.table_id, r.system_name);
    if (st.profile) window.Shell.openTab("/ui/profiler/" + r.table_id);
    if (st.studio) window.Shell.openTab("/ui/studio/" + r.table_id);
    if (st.target) setTimeout(openTarget, 0);
  }

  async function openSource() {
    const dialects = (await run(() => api("GET", "/api/connections/dialects"))) || [];
    const presets = (await run(() => api("GET", "/api/connections/presets"))) || [];
    open({ title: "Add Source Connection Wizard", steps: sourceSteps, guide: true, finish: (st) => (st.kind === "file" ? finishSource(st) : isStorage(st) ? finishStorage(st) : finishDb(st)),
      cancel: async (st) => {                                  // a connection saved for browsing is removed again if the wizard is abandoned
        st.conn.password = "";
        for (const n of st.createdNames) {
          if (st.finished && n === st.name.trim()) continue;       // the finished source keeps its connection
          try { await api("DELETE", "/api/connections/" + encodeURIComponent(n)); } catch (e) { /* already gone */ }
        }
      },
      init: () => ({ kind: "file", dialects, presets, preset: null, name: "", opt: { header: true }, file: null, preview: null, detected: "", table: "", business: "", expected: "", keys: new Set(), profile: true, studio: true, target: false,
        conn: newConn(), dbtest: null, connSaved: false, createdNames: [], finished: false, selectedFiles: new Map(), container: "", prefix: "", schemasList: null, tablesBySchema: {}, openSchemas: new Set(), selected: new Map(), filter: "", hideSystem: true, control: true, results: new Map() }) });
  }

  // ================================================================== ADD TARGET
  const sapTargetSteps = [
    {
      label: "Select provider", desc: "Select where the migrated data will be loaded and give the target a name.",
      help: "Pick the sandbox to rehearse safely, the Migration Cockpit workbook to hand data to SAP's own tool, or the live OData API to create business partners directly.",
      render(el, st) {
        const kinds = (window.Shell.targets() || { targets: [] }).targets;
        el.innerHTML = '<fieldset><legend>Target name</legend>' + row("Name:", '<input type="text" id="g-name" maxlength="128" value="' + esc(st.name) + '" autocomplete="off">') + "</fieldset>" +
          '<fieldset><legend>Data Provider(s)</legend>' +
          row("Database:", '<select id="g-db"><option selected>SAP S/4HANA</option><optgroup label="Coming soon">' + ["SAP HANA (database)", "Oracle Database", "Microsoft SQL Server"].map((c) => "<option disabled>" + esc(c) + "</option>").join("") + "</optgroup></select>") +
          row("Provider:", '<select id="g-kind">' + kinds.map((t) => '<option value="' + esc(t.kind) + '"' + (t.kind === st.kind ? " selected" : "") + ">" + esc(t.label) + (t.available ? "" : " (not configured)") + "</option>").join("") + "</select>",
            (kinds.find((t) => t.kind === st.kind) || {}).detail || "") + "</fieldset>";
        $("#g-name").addEventListener("input", (e) => { st.name = e.target.value; refreshNav(); });
        $("#g-kind").addEventListener("change", (e) => { st.kind = e.target.value; st.test = null; renderStep(); });
      },
      gate: (st) => (st.name.trim() ? null : "Enter a name for the target"),
    },
    {
      label: "Set connection", desc: "Check the connection to the target. Credentials are held by the server and are never shown here.",
      help: "For the live system the server reads SAP_BASE_URL, SAP_USER and SAP_PASSWORD (and optionally SAP_CLIENT) from its environment. Test authenticates and fetches a CSRF token; it creates nothing in SAP.",
      render(el, st) {
        const t = (window.Shell.targets() || { targets: [] }).targets.find((x) => x.kind === st.kind) || {};
        const rows = [["Provider", t.label || st.kind], ["Mode", t.simulated ? "Simulated (nothing external is contacted)" : st.kind === "cockpit" ? "File export" : "Live connection"], ["Status", t.detail || ""]];
        el.innerHTML = '<div class="pg-wrap"><table class="pg"><tbody><tr class="grp"><td colspan="2">&#9662; Connection properties</td></tr>' +
          rows.map(([k, v]) => "<tr><td>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("") + "</tbody></table></div>" +
          '<div class="wz-actionrow"><span class="wz-status' + (st.test ? (st.test.ok ? " ok" : " bad") : "") + '" id="g-status">' +
          esc(st.kind === "cockpit" ? "No connection is needed for the workbook export." : st.test ? (st.test.ok ? "Test succeeded: " : "Test failed: ") + st.test.detail + " (" + st.test.latency_ms + " ms)" : "Press Test to check the connection.") + "</span>" +
          '<button type="button" class="wz-btn" id="g-test"' + (t.testable ? "" : " disabled") + ">Test</button></div>";
        $("#g-test").addEventListener("click", async () => {
          $("#g-status").className = "wz-status"; $("#g-status").textContent = "Testing...";
          try { st.test = await api("POST", "/api/targets/test", { target: st.kind }); } catch (e) { st.test = { ok: false, detail: e.message, latency_ms: 0 }; }
          renderStep();
        });
      },
      gate: (st) => (st.kind === "cockpit" || (st.test && st.test.ok) ? null : "Press Test and get a successful result first"),
    },
    {
      label: "Select object", desc: "Choose the business object to load. Its target tables and fields come from the SAP catalog.",
      help: "The catalog lists the SAP tables and fields (mandatory flags, lengths, check tables) that mappings and validation are checked against.",
      render(el, st) {
        const ready = (window.Shell.targets() || {}).catalog_ready;
        el.innerHTML = '<fieldset><legend>Business object</legend><div class="wz-cols"><table><thead><tr><th>Object</th><th>Tables</th><th>Catalog</th></tr></thead><tbody><tr><td>Business Partner (BUSINESS_PARTNER)</td>' +
          "<td>BUT000, BUT100, BUT020, ADRC, KNB1, KNVV</td><td>" + (ready ? '<span class="badge green">seeded</span>' : '<span class="badge amber">not seeded</span>') + "</td></tr></tbody></table></div>" +
          (ready ? "" : '<div class="wz-actionrow"><span class="wz-status bad">The target catalog has not been loaded yet.</span><button type="button" class="wz-btn" id="g-seed">Seed catalog</button></div>') + "</fieldset>";
        const seed = $("#g-seed");
        if (seed) seed.addEventListener("click", async () => {
          seed.disabled = true;
          try { await api("POST", "/api/admin/seed-sap-catalog"); await window.Shell.refresh(); renderStep(); } catch (e) { showError(e.message); seed.disabled = false; }
        });
      },
      gate: () => ((window.Shell.targets() || {}).catalog_ready ? null : "Seed the SAP target catalog first"),
    },
    {
      label: "Actions", desc: "Choose the action(s) to perform at the end of the wizard.",
      render(el, st) {
        el.innerHTML = '<div class="wz-checks"><div>At the end of the wizard:</div>' +
          '<label><input type="checkbox" id="g-src"' + (st.source ? " checked" : "") + '> Launch the Add Source Connection Wizard</label><div class="sub">Continue with the legacy data that will be loaded here.</div></div>';
        $("#g-src").addEventListener("change", (e) => { st.source = e.target.checked; });
      },
    },
    {
      label: "Summary", desc: "Check that the target connection contains the correct values.",
      render(el, st) {
        const t = (window.Shell.targets() || { targets: [] }).targets.find((x) => x.kind === st.kind) || {};
        el.innerHTML = '<fieldset><legend>Target summary</legend>' + summary([
          ["Target name", st.name.trim()], ["Provider", t.label], ["Mode", t.simulated ? "Simulated" : st.kind === "cockpit" ? "File export" : "Live"],
          ["Connection", st.kind === "cockpit" ? "not needed" : st.test ? "tested OK (" + st.test.latency_ms + " ms)" : "not tested"], ["Business object", "BUSINESS_PARTNER (catalog seeded)"]]) + "</fieldset>" +
          '<fieldset><legend>Click Finish to complete</legend>' + summary([["", "Nothing is created or changed. The load mode and target are chosen per run, in the Governance hub."], st.source ? ["", "Launch the Add Source Connection Wizard"] : null]) + "</fieldset>";
      },
    },
  ];

  // ------------------------------------------------------------------ database targets
  const dbTargetProvider = (el, st) => {
    el.innerHTML = '<fieldset><legend>Target name</legend>' + row("Name:", '<input type="text" id="g-name" maxlength="64" value="' + esc(st.name) + '" autocomplete="off">', "Letters, digits, '.', '_' and '-'.") + "</fieldset>" +
      '<fieldset><legend>Data Provider(s)</legend>' +
      row("Database:", '<select id="g-dialect"><optgroup label="Databases">' + kindOptions(st, st.kind, true) + "</optgroup></select>") +
      row("Provider:", '<input type="text" readonly value="' + esc(DRIVERS[st.kind] || "") + '">', "Use an account that may create tables in one schema. Nothing is ever dropped except a table this application created and you chose to replace.") + "</fieldset>";
    $("#g-name").addEventListener("input", (e) => { st.name = e.target.value; st.dbtest = null; st.connSaved = false; st.writeTest = null; refreshNav(); });
    $("#g-dialect").addEventListener("change", (e) => { applyPreset(st, e.target.value); st.dbtest = null; st.connSaved = false; st.writeTest = null; renderStep(); });
  };

  const dbSchemaStep = {
    label: "Select object", desc: "Choose the schema where new tables are created, and prove this account may write there.",
    help: "Test write access creates a scratch table in the schema, inserts and reads a row, then drops it. Delivering a run later creates one new table per delivery and never changes existing tables unless you choose to replace one.",
    render(el, st) {
      const name = st.name.trim();
      el.innerHTML = '<fieldset><legend>Schema</legend>' + row("Schema:", '<select id="t-schema"><option>Loading...</option></select>', "New tables are created here.") + "</fieldset>" +
        '<div class="wz-actionrow"><span class="wz-status' + (st.writeTest ? (st.writeTest.ok ? " ok" : " bad") : "") + '" id="t-status">' +
        esc(st.writeTest ? st.writeTest.detail : "Press Test write access.") + '</span><button type="button" class="wz-btn" id="t-test" disabled>Test write access</button></div>';
      const sel = $("#t-schema");
      const fill = (list) => {
        st.schemas = list;
        const mine = list.find((x) => x.toLowerCase() === st.conn.user.trim().toLowerCase());
        if (!st.schema || !list.includes(st.schema)) st.schema = mine || list[0] || "";
        sel.innerHTML = list.map((x) => '<option' + (x === st.schema ? " selected" : "") + ">" + esc(x) + "</option>").join("") || "<option value=''>(no schema visible)</option>";
        $("#t-test").disabled = !st.schema; refreshNav();
      };
      if (st.schemas) fill(st.schemas);
      else api("GET", "/api/connections/" + encodeURIComponent(name) + "/schemas").then(fill).catch((e) => { sel.innerHTML = "<option value=''>-</option>"; showError(e.message); });
      sel.addEventListener("change", () => { st.schema = sel.value; st.writeTest = null; $("#t-status").className = "wz-status"; $("#t-status").textContent = "Press Test write access."; refreshNav(); });
      $("#t-test").addEventListener("click", async () => {
        $("#t-status").className = "wz-status"; $("#t-status").textContent = "Testing...";
        try { st.writeTest = Object.assign({ schema: st.schema }, await api("POST", "/api/connections/" + encodeURIComponent(name) + "/test-write", { schema_name: st.schema })); }
        catch (e) { st.writeTest = { ok: false, detail: e.message, schema: st.schema }; }
        renderStep();
      });
    },
    gate: (st) => (st.writeTest && st.writeTest.ok && st.writeTest.schema === st.schema ? null : "Press Test write access and get a successful result first"),
  };

  const dbTargetSummary = {
    label: "Summary", desc: "Check that the target connection contains the correct values.",
    render(el, st) {
      const d = st.dialects.find((x) => x.dialect === st.kind) || {};
      el.innerHTML = '<fieldset><legend>Target summary</legend>' + summary([
        ["Target name", st.name.trim()], ["Provider", d.label || st.kind], ["Server", st.conn.host.trim() + ":" + (st.conn.port || d.default_port)], [d.database_label || "Database", st.conn.database.trim() || "(default)"],
        ["User", st.conn.user.trim()], ["Schema for new tables", st.schema], ["Write access", st.writeTest ? "verified (" + st.writeTest.detail + ")" : "not tested"]]) + "</fieldset>" +
        '<fieldset><legend>Click Finish to complete</legend>' + summary([["", "Save this as a target connection (no password is stored)"],
          ["", "Runs that pass the governance gate and are signed off can then be delivered into a new table here, from the Governance hub"], st.source ? ["", "Launch the Add Source Connection Wizard"] : null]) + "</fieldset>";
    },
  };

  const targetSteps = sapTargetSteps.map((ss, i) => {
    const isDb = (st) => st.type === "db";
    const dbStep = [null, dbConnectionStep, dbSchemaStep, null, dbTargetSummary][i];
    const pick = (st) => (isDb(st) && dbStep ? dbStep : ss);
    return {
      label: ss.label, desc: ss.desc, help: ss.help,
      render: (el, st, w) => {
        if (i === 0) {
          el.innerHTML = '<fieldset><legend>Target type</legend>' + row("Type:", '<select id="g-type"><option value="sap"' + (st.type === "sap" ? " selected" : "") + ">SAP S/4HANA (sandbox, Migration Cockpit, live OData)</option>" +
            '<option value="db"' + (st.type === "db" ? " selected" : "") + ">Database (write access): Oracle, SQL Server, PostgreSQL, MySQL</option></select>") + '</fieldset><div id="g-rest"></div>';
          $("#g-type").addEventListener("change", (e) => {
            st.type = e.target.value; st.role = st.type === "db" ? "target" : undefined;
            if (st.type === "db") { st.kind = st.kind === "sandbox" || st.kind === "cockpit" || st.kind === "live" ? "postgresql" : st.kind; if (st.name === "SAP-S4HANA") st.name = ""; }
            else { st.kind = "sandbox"; if (!st.name) st.name = "SAP-S4HANA"; }
            st.dbtest = null; st.connSaved = false; st.writeTest = null; renderStep();
          });
          if (st.type === "db") dbTargetProvider($("#g-rest"), st); else ss.render($("#g-rest"), st, w);
          return;
        }
        const step = pick(st);
        $("#wz-desc").innerHTML = esc(step.desc || ss.desc) + (w.showHelp && (step.help || ss.help) ? '<span class="more">' + esc(step.help || ss.help) + "</span>" : "");
        step.render(el, st, w);
      },
      gate: (st) => {
        if (i === 0) return !st.name.trim() ? "Enter a name for the target" : st.type === "db" && !/^[A-Za-z0-9_.-]{1,64}$/.test(st.name.trim()) ? "A database target name may use letters, digits, '.', '_' and '-' only (up to 64)" : null;
        const step = pick(st); return step.gate ? step.gate(st) : null;
      },
    };
  });

  async function finishDbTarget(st) {
    const name = st.name.trim();
    await api("POST", "/api/connections", Object.assign({ name, role: "target", default_schema: st.schema, password_env: st.conn.password_env.trim() || undefined }, connBody(st)));
    st.conn.password = ""; st.finished = true;
    for (const n of st.createdNames) { if (n !== name) { try { await api("DELETE", "/api/connections/" + encodeURIComponent(n)); } catch (e) { /* already gone */ } } }
    await window.Shell.refresh();
    App.toast("Target " + name + " saved: new tables will be created in schema " + st.schema);
    if (st.source) setTimeout(openSource, 0);
  }

  async function openTarget() {
    const dialects = (await run(() => api("GET", "/api/connections/dialects"))) || [];
    const presets = (await run(() => api("GET", "/api/connections/presets"))) || [];
    open({ title: "Add Target Connection Wizard", steps: targetSteps,
      finish: async (st) => {
        if (st.type === "db") return finishDbTarget(st);
        await window.Shell.refresh(); App.toast("Target " + st.name.trim() + " checked: " + st.kind + (st.test ? " (" + st.test.detail + ")" : "")); if (st.source) setTimeout(openSource, 0);
      },
      cancel: async (st) => {
        st.conn.password = "";
        for (const n of st.createdNames) { try { await api("DELETE", "/api/connections/" + encodeURIComponent(n)); } catch (e) { /* already gone */ } }
      },
      init: () => ({ name: "SAP-S4HANA", type: "sap", kind: "sandbox", test: null, source: false, role: undefined, dialects, presets, preset: null, conn: newConn(), dbtest: null, connSaved: false,
        createdNames: [], finished: false, schema: "", schemas: null, writeTest: null }) });
  }

  // ================================================================== the setup guide beside the Add Source wizard
  // It is told what the step holds (never the password), says what the step needs, explains a failed test, and fills the form in from what the person typed.
  const STEP_KEYS = ["provider", "connection", "tables", "options", "summary"];
  let guide = null;
  function guideContext() {
    if (!wz || !wz.guide) return { step: "start" };
    const st = wz.state, c = st.conn || {}, step = STEP_KEYS[wz.i] || "summary";
    const tables = [];
    Object.keys(st.tablesBySchema || {}).forEach((sc) => (st.tablesBySchema[sc] || []).forEach((t) => tables.push({ name: sc + "." + t.name, rows: t.row_estimate === undefined ? null : t.row_estimate, kind: t.kind || "" })));
    const error = st.kind === "file" ? (st.lastError || "") : (st.dbtest && !st.dbtest.ok ? st.dbtest.detail || "" : "");
    return { step, key: step + ":" + (st.kind || ""), kind: st.kind || "file", error: error.slice(0, 600), file_name: st.file ? st.file.name : "", tables: tables.slice(0, 300),
      fields: { name: st.name || "", kind: st.kind || "", host: c.host || "", port: c.port || "", database: c.database || "", user: c.user || "", file: st.file ? st.file.name : "",
        detected: st.kind === "file" && st.preview ? st.detected || "" : "", tables: (st.selected && st.selected.size) || (st.selectedFiles && st.selectedFiles.size) ? "yes" : "" } };
  }
  function syncGuide() {
    if (!guide || !wz || !wz.guide) return;
    const c = guideContext(), seen = (wz.guided = wz.guided || { error: "", detected: "" });
    if (c.error && c.error !== seen.error) { seen.error = c.error; guide.forceRefresh(); return; }
    if (!c.error) seen.error = "";
    if (c.fields.detected && c.fields.detected !== seen.detected) { seen.detected = c.fields.detected; guide.forceRefresh(); return; }
    guide.refresh();
  }
  function tablesDrawn() {
    if (!guide || !wz || !wz.guide || wz.i !== 2) return;
    const names = Object.keys(wz.state.tablesBySchema || {}).flatMap((sc) => (wz.state.tablesBySchema[sc] || []).map((t) => sc + "." + t.name)).join(",");
    const seen = (wz.guided = wz.guided || { error: "", detected: "" });
    if (names && names !== seen.tables) { seen.tables = names; guide.forceRefresh(); }
  }
  function applyFill(fill) {
    if (!wz || !wz.guide) return;
    const st = wz.state, c = st.conn;
    let kindChanged = false;
    if (fill.kind && fill.kind !== st.kind && !(st.dbtest && st.dbtest.ok)) {
      if (fill.kind === "file") { st.kind = "file"; st.preset = null; } else applyPreset(st, fill.kind);
      st.preview = null; st.dbtest = null; st.connSaved = false; kindChanged = true;
    }
    if (fill.name) st.name = fill.name;
    let touched = false;
    ["host", "port", "database", "user"].forEach((k) => { if (fill[k] !== undefined) { c[k] = fill[k]; touched = true; } });
    if (touched) { st.dbtest = null; st.connSaved = false; }
    if (kindChanged && wz.i > 1) wz.i = 1;                           // the provider changed: back to the connection details of the new one
    if (fill.pick_tables && wz.i === 2) {                            // the list is already on the screen: tick in place, do not redraw it
      fill.pick_tables.split(",").forEach((full) => {
        const [schema, ...rest] = full.split("."), table = rest.join(".");
        const box = document.querySelector('#wz-body input[data-schema="' + schema + '"][data-table="' + table + '"]');
        if (box && !box.checked && !box.disabled) { box.checked = true; box.dispatchEvent(new Event("change", { bubbles: true })); }
      });
      return;
    }
    renderStep();
  }
  async function attachFile(file) {
    if (!wz || !wz.guide) { await openSource(); }
    const st = wz.state;
    st.kind = "file"; st.preset = null; st.file = file; st.preview = null; st.table = ""; st.keys = new Set(); st.lastError = "";
    if (!st.name.trim()) st.name = defaultTableName(file.name);
    wz.i = 1; showError(""); renderStep();
    await readFile(st, $("#p-status"), () => renderStep());
  }
  guide = window.Guide && $("#wz-guide") ? Guide.mount($("#wz-guide"), { context: guideContext, onFill: applyFill, onFile: attachFile }) : null;

  window.Wizards = { source: openSource, target: openTarget, attachFile, fill: (f) => { if (wz && wz.guide) applyFill(f); }, openSourceWith: async (fill, file) => { await openSource(); if (fill) applyFill(fill); if (file) await attachFile(file); } };
})();
