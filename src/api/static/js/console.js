/* Management Center shell: explorer tree, tabbed documents (each page is a same-origin iframe), menus, toolbar, status bar.
   Every dynamic value that reaches innerHTML goes through App.esc(). */
(function () {
  "use strict";
  const { $, esc, api, run } = App;
  const LS = { tabs: "migration.console.tabs", open: "migration.console.open", width: "migration.console.explorer" };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };

  // ------------------------------------------------------------------ data
  let tables = [], sets = [], runs = [], health = null, targets = null;
  const tableName = (id) => (tables.find((t) => t.table_id === Number(id)) || {}).table_name;

  // ------------------------------------------------------------------ tabs
  const tabs = [];            // {url, title, icon, frame, tab}
  let active = null;

  function describe(url) {
    const path = url.split("?")[0];
    let m;
    if (path === "/ui/start") return { title: "Start Page", icon: "i-home" };
    if (path === "/ui/sources") return { title: "Sources & Runs", icon: "i-upload" };
    if ((m = path.match(/^\/ui\/profiler\/(\d+)$/))) return { title: "Profiler - " + (tableName(m[1]) || "table " + m[1]), icon: "i-chart" };
    if ((m = path.match(/^\/ui\/studio\/(\d+)$/))) return { title: "Mapping - " + (tableName(m[1]) || "table " + m[1]), icon: "i-map" };
    if ((m = path.match(/^\/ui\/governance\/(\d+)$/))) return { title: "Governance - run " + m[1], icon: "i-shield" };
    if ((m = path.match(/^\/ui\/certificate\/(\d+)$/))) return { title: "Certificate - run " + m[1], icon: "i-cert" };
    return { title: path, icon: "i-page" };
  }
  const frameUrl = (url) => url + (url.includes("?") ? "&" : "?") + "embed=1";

  function openTab(url, title) {
    if (!/^\/ui\/[a-z]+(\/\d+)?$/.test(url)) return;              // only our own pages are ever hosted
    let t = tabs.find((x) => x.url === url);
    if (!t) {
      const d = describe(url);
      const frame = document.createElement("iframe");
      frame.title = d.title; frame.src = frameUrl(url);
      $("#tabbody").appendChild(frame);
      t = { url, title: d.title, icon: d.icon, frame };
      tabs.push(t);
    }
    activate(t);
  }

  function activate(t) {
    active = t;
    tabs.forEach((x) => x.frame.classList.toggle("active", x === t));
    renderTabs(); persistTabs(); updateToolbar();
  }

  function closeTab(t) {
    const i = tabs.indexOf(t);
    if (i < 0) return;
    t.frame.remove(); tabs.splice(i, 1);
    if (active === t) active = tabs[Math.min(i, tabs.length - 1)] || null;
    if (active) activate(active); else { renderTabs(); persistTabs(); updateToolbar(); }
  }

  function renderTabs() {
    const strip = $("#tabstrip");
    strip.innerHTML = tabs.map((t, i) =>
      '<div class="tab" role="tab" tabindex="0" data-i="' + i + '" aria-selected="' + (t === active) + '" title="' + esc(t.title) + '">' +
      '<svg class="ic"><use href="#' + esc(t.icon) + '"/></svg><span class="title">' + esc(t.title) + "</span>" +
      '<button type="button" class="x" aria-label="Close tab" data-close="' + i + '"><svg class="ic"><use href="#i-close"/></svg></button></div>').join("");
    if (!tabs.length) $("#tabbody").dataset.empty = "1"; else delete $("#tabbody").dataset.empty;
    let msg = $("#empty-docs");
    if (!tabs.length && !msg) { msg = document.createElement("div"); msg.id = "empty-docs"; msg.className = "empty-docs"; msg.textContent = "No documents open. Double-click an item in the explorer, or choose File > Start Page."; $("#tabbody").appendChild(msg); }
    if (tabs.length && msg) msg.remove();
    $("#window-menu").innerHTML = tabs.length ? tabs.map((t, i) =>
      '<button type="button" role="menuitem" class="' + (t === active ? "check" : "") + '" data-act="goto-tab" data-i="' + i + '">' + esc(t.title) + "</button>").join("")
      : '<button type="button" disabled>No open tabs</button>';
  }
  const persistTabs = () => store.set(LS.tabs, { urls: tabs.map((t) => t.url), active: active && active.url });

  $("#tabstrip").addEventListener("click", (e) => {
    const x = e.target.closest("[data-close]");
    if (x) { e.stopPropagation(); closeTab(tabs[Number(x.dataset.close)]); return; }
    const tab = e.target.closest(".tab"); if (tab) activate(tabs[Number(tab.dataset.i)]);
  });
  $("#tabstrip").addEventListener("auxclick", (e) => {
    const tab = e.target.closest(".tab"); if (tab && e.button === 1) { e.preventDefault(); closeTab(tabs[Number(tab.dataset.i)]); }
  });
  $("#tabstrip").addEventListener("keydown", (e) => {
    const tab = e.target.closest(".tab");
    if (tab && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); activate(tabs[Number(tab.dataset.i)]); }
  });

  // pages ask the console to open another tab or to refresh after they changed something
  let refreshTimer = null;
  window.addEventListener("message", (e) => {
    if (e.origin !== window.location.origin || !e.data) return;
    if (e.data.type === "open-tab" && typeof e.data.url === "string") openTab(e.data.url, e.data.title);
    if (e.data.type === "changed") { clearTimeout(refreshTimer); refreshTimer = setTimeout(loadExplorer, 400); }
  });

  // ------------------------------------------------------------------ explorer tree
  let nodes = [];            // flat list of {id, depth, parent, label, ...} in display order, rebuilt on every refresh
  let selected = store.get("migration.console.selected", null);
  const openSet = new Set(store.get(LS.open, ["root", "sources"]));

  function badgeDot(status) {
    const s = String(status || "").toUpperCase();
    const kind = { APPROVED: "green", SUCCEEDED: "green", READY_FOR_LOAD: "green", DRAFT: "gray", SUBMITTED: "amber", RUNNING: "amber", REJECTED: "red", FAILED: "red", BLOCKED: "red" }[s] || "gray";
    return '<span class="dot ' + kind + '" title="' + esc(status) + '"></span>';
  }

  function buildModel() {
    const bySystem = {};
    tables.forEach((t) => { (bySystem[t.system_name] = bySystem[t.system_name] || []).push(t); });
    const setsOf = (tableId) => sets.filter((m) => m.source_table_id === tableId);
    const tableNode = (t) => ({
      id: "t" + t.table_id, icon: "i-table", label: t.table_name, hint: t.row_count_estimate != null ? Number(t.row_count_estimate).toLocaleString() + " rows" : "",
      ctx: { table: t.table_id }, open: "/ui/profiler/" + t.table_id,
      children: [
        { id: "t" + t.table_id + "p", icon: "i-chart", label: "Profiler", ctx: { table: t.table_id }, open: "/ui/profiler/" + t.table_id },
        { id: "t" + t.table_id + "m", icon: "i-map", label: "Mapping Studio", ctx: { table: t.table_id }, open: "/ui/studio/" + t.table_id },
        ...(setsOf(t.table_id).length ? [{ id: "t" + t.table_id + "s", icon: "i-folder", label: "Mapping sets", hint: String(setsOf(t.table_id).length),
          ctx: { table: t.table_id }, children: setsOf(t.table_id).map((m) => ({ id: "m" + m.mapping_set_id, icon: "i-map", label: "Set #" + m.mapping_set_id + " v" + m.version + " " + m.status.toLowerCase(),
            dot: m.status, ctx: { table: t.table_id }, open: "/ui/studio/" + t.table_id })) }] : []),
      ],
    });
    return {
      id: "root", icon: "i-db", label: "ERP Migration Accelerator", root: true, open: "/ui/start", children: [
        { id: "sources", icon: "i-folder", label: "Sources", hint: tables.length ? String(tables.length) : "", open: "/ui/sources",
          children: Object.keys(bySystem).sort().map((sys) => ({ id: "sys:" + sys, icon: "i-folder", label: sys, hint: String(bySystem[sys].length), children: bySystem[sys].map(tableNode) })) },
        { id: "targets", icon: "i-folder", label: "Targets", children: targets ? targets.targets.map((t) => ({
          id: "target-" + t.kind, icon: "i-db", label: t.label, dot: t.available ? "APPROVED" : "DRAFT", hint: t.available ? "ready" : "not configured", action: "new-target" }))
          .concat((targets.databases || []).map((d) => ({ id: "targetdb-" + d.name, icon: "i-db", label: d.name, dot: d.password_source === "missing" ? "DRAFT" : "APPROVED",
            hint: d.label + (d.default_schema ? " / " + d.default_schema : "") + (d.password_source === "missing" ? " - password needed" : ""), action: "new-target" }))) : [] },
        { id: "runs", icon: "i-folder", label: "Runs", hint: runs.length ? String(runs.length) : "",
          children: runs.slice(0, 40).map((r) => ({ id: "r" + r.run_id, icon: "i-run", label: "Run " + r.run_id + " - " + r.wave_name, dot: r.status, hint: r.status.toLowerCase(),
            ctx: { run: r.run_id }, open: "/ui/governance/" + r.run_id,
            children: [
              { id: "r" + r.run_id + "g", icon: "i-shield", label: "Governance hub", ctx: { run: r.run_id }, open: "/ui/governance/" + r.run_id },
              { id: "r" + r.run_id + "c", icon: "i-cert", label: "Reconciliation certificate", ctx: { run: r.run_id }, open: "/ui/certificate/" + r.run_id },
            ] })) },
        { id: "settings", icon: "i-folder", label: "Settings", children: [
          { id: "set-keys", icon: "i-key", label: "Gemini API keys", action: "keys" },
        ] },
      ],
    };
  }

  function renderTree() {
    const model = buildModel();
    nodes = [];
    const html = (n, depth, parent) => {
      nodes.push({ id: n.id, n, parent });
      const kids = n.children || [];
      const isOpen = openSet.has(n.id);
      let s = '<li role="treeitem"' + (kids.length ? ' aria-expanded="' + isOpen + '"' : "") + ' data-id="' + esc(n.id) + '">' +
        '<div class="node' + (isOpen ? " open" : "") + (n.root ? " root" : "") + (n.id === selected ? " selected" : "") + '" tabindex="' + (n.id === selected ? 0 : -1) + '">' +
        '<span class="twist' + (kids.length ? "" : " leaf") + '" data-twist></span><svg class="ic"><use href="#' + esc(n.icon) + '"/></svg>' +
        '<span class="label">' + esc(n.label) + "</span>" + (n.dot ? badgeDot(n.dot) : "") + (n.hint ? '<span class="hint">' + esc(n.hint) + "</span>" : "") + "</div>";
      if (kids.length) s += '<ul role="group"' + (isOpen ? "" : " hidden") + ">" + kids.map((k) => html(k, depth + 1, n.id)).join("") + "</ul>";
      return s + "</li>";
    };
    $("#tree").innerHTML = html(model, 0, null);
    if (!nodes.some((x) => x.id === selected)) selected = null;
    updateToolbar();
  }

  const nodeById = (id) => (nodes.find((x) => x.id === id) || {}).n;
  function select(id) {
    selected = id; store.set("migration.console.selected", id);
    document.querySelectorAll("#tree .node").forEach((el) => {
      const on = el.parentElement.dataset.id === id;
      el.classList.toggle("selected", on); el.tabIndex = on ? 0 : -1;
    });
    updateToolbar();
  }
  function toggle(id, force) {
    const n = nodeById(id); if (!n || !(n.children || []).length) return;
    const want = force === undefined ? !openSet.has(id) : force;
    if (want) openSet.add(id); else openSet.delete(id);
    store.set(LS.open, [...openSet]);
    const li = document.querySelector('#tree li[data-id="' + CSS.escape(id) + '"]');
    if (!li) return;
    li.setAttribute("aria-expanded", String(want));
    li.firstElementChild.classList.toggle("open", want);
    const ul = li.querySelector(":scope > ul"); if (ul) ul.hidden = !want;
  }
  function openNode(id) {
    const n = nodeById(id); if (!n) return;
    if (n.action) return act(n.action);
    if (n.open) openTab(n.open); else toggle(id);
  }
  function visibleIds() {
    return [...document.querySelectorAll("#tree .node")].filter((el) => !el.closest("ul[hidden]")).map((el) => el.parentElement.dataset.id);
  }

  $("#tree").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]"); if (!li) return;
    const id = li.dataset.id;
    if (e.target.closest("[data-twist]")) { toggle(id); return; }
    select(id);
  });
  $("#tree").addEventListener("dblclick", (e) => {
    const li = e.target.closest("li[data-id]"); if (!li || e.target.closest("[data-twist]")) return;
    const n = nodeById(li.dataset.id);
    if (n && !n.open && !n.action) toggle(li.dataset.id); else openNode(li.dataset.id);
  });
  $("#tree").addEventListener("keydown", (e) => {
    if (!selected) return;
    const ids = visibleIds(), i = ids.indexOf(selected), n = nodeById(selected);
    const go = (id) => { if (id) { select(id); const el = document.querySelector('#tree li[data-id="' + CSS.escape(id) + '"] > .node'); if (el) el.focus(); } };
    if (e.key === "ArrowDown") { e.preventDefault(); go(ids[i + 1]); }
    else if (e.key === "ArrowUp") { e.preventDefault(); go(ids[i - 1]); }
    else if (e.key === "ArrowRight") { e.preventDefault(); if ((n.children || []).length && !openSet.has(selected)) toggle(selected, true); else go(ids[i + 1]); }
    else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if ((n.children || []).length && openSet.has(selected)) toggle(selected, false); else go((nodes.find((x) => x.id === selected) || {}).parent);
    } else if (e.key === "Enter") { e.preventDefault(); openNode(selected); }
  });

  async function loadExplorer() {
    const [t, s, r, h, g] = await Promise.all([
      run(() => api("GET", "/api/tables")), run(() => api("GET", "/api/mapping-sets")), run(() => api("GET", "/api/runs")), run(() => api("GET", "/api/health")),
      run(() => api("GET", "/api/targets")),
    ]);
    tables = t || tables; sets = s || sets; runs = r || runs; health = h || health; targets = g || targets;
    renderTree(); renderStatus(); tabs.forEach(retitle);
  }
  function retitle(t) {
    const d = describe(t.url); t.title = d.title; t.frame.title = d.title;
    renderTabs();
  }

  // ------------------------------------------------------------------ status bar
  function renderStatus() {
    const db = $("#st-db");
    if (health) {
      const ok = health.database === "ok";
      db.className = "st " + (ok ? "ok" : "bad");
      db.innerHTML = '<svg class="ic"><use href="#i-db"/></svg><span>Database: ' + esc(health.database) + "</span>";
      $("#st-auth").textContent = "Auth: " + health.auth_mode;
      $("#st-target").textContent = "SAP target: " + health.sap_default_target + " (simulated)" + (health.target_catalog_ready ? "" : " - catalog not seeded");
      $("#st-user").textContent = "AI: " + (health.llm_provider || "-");
    } else { db.className = "st bad"; db.innerHTML = '<svg class="ic"><use href="#i-db"/></svg><span>Server not reachable</span>'; }
  }

  // ------------------------------------------------------------------ toolbar + menus
  function selCtx() { const n = selected && nodeById(selected); return (n && n.ctx) || {}; }
  function updateToolbar() {
    const c = selCtx();
    // the open tab also counts as context, so Profiler/Governance work for whatever you are looking at
    const m = active && active.url.match(/^\/ui\/(profiler|studio|governance|certificate)\/(\d+)$/);
    const table = c.table || (m && /profiler|studio/.test(m[1]) ? Number(m[2]) : null);
    const runId = c.run || (m && /governance|certificate/.test(m[1]) ? Number(m[2]) : null);
    document.querySelectorAll("[data-needs]").forEach((b) => { b.disabled = !(b.dataset.needs === "table" ? table : runId); });
    updateToolbar.table = table; updateToolbar.run = runId;
  }

  const ACTIONS = {
    "open-start": () => openTab("/ui/start"),
    "open-sources": () => openTab("/ui/sources"),
    "new-source": () => window.Wizards && window.Wizards.source(),
    "new-target": () => window.Wizards && window.Wizards.target(),
    "open-profiler": () => updateToolbar.table && openTab("/ui/profiler/" + updateToolbar.table),
    "open-studio": () => updateToolbar.table && openTab("/ui/studio/" + updateToolbar.table),
    "open-governance": () => updateToolbar.run && openTab("/ui/governance/" + updateToolbar.run),
    "open-certificate": () => updateToolbar.run && openTab("/ui/certificate/" + updateToolbar.run),
    "open-api": () => window.open("/docs", "_blank", "noopener"),
    refresh: () => { loadExplorer(); tabs.forEach((t) => { t.frame.src = frameUrl(t.url); }); },
    "toggle-explorer": () => $("#workbench").classList.toggle("explorer-hidden"),
    "expand-all": () => { nodes.forEach((x) => { if ((x.n.children || []).length) openSet.add(x.id); }); store.set(LS.open, [...openSet]); renderTree(); },
    "collapse-all": () => { openSet.clear(); openSet.add("root"); store.set(LS.open, [...openSet]); renderTree(); },
    "close-tab": () => active && closeTab(active),
    "close-all": () => { [...tabs].forEach(closeTab); },
    keys: () => $("#key-indicator").click(),
    seed: () => run(async () => { await api("POST", "/api/admin/seed-sap-catalog"); App.toast("SAP target catalog seeded"); loadExplorer(); }),
    shortcuts: () => $("#shortcuts-modal").showModal(),
    "goto-tab": (el) => tabs[Number(el.dataset.i)] && activate(tabs[Number(el.dataset.i)]),
  };
  function act(name, el) { const fn = ACTIONS[name]; if (fn) fn(el); }

  document.addEventListener("click", (e) => {
    const item = e.target.closest("[data-act]");
    const menu = e.target.closest(".menu");
    const wasOpen = menu && menu.classList.contains("open");
    document.querySelectorAll(".menu.open").forEach((m) => m.classList.remove("open"));
    if (e.target.closest(".menu-btn") && menu) { if (!wasOpen) menu.classList.add("open"); return; }
    if (item && !item.disabled) act(item.dataset.act, item);
  });
  document.addEventListener("mouseover", (e) => {      // while one menu is open, hovering another switches to it
    const menu = e.target.closest(".menu"), openMenu = document.querySelector(".menu.open");
    if (menu && openMenu && menu !== openMenu) { openMenu.classList.remove("open"); menu.classList.add("open"); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".menu.open").forEach((m) => m.classList.remove("open"));
    if (e.altKey && e.key.toLowerCase() === "w") { e.preventDefault(); ACTIONS["close-tab"](); }
    if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft") && tabs.length) {
      e.preventDefault();
      const i = tabs.indexOf(active); activate(tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]);
    }
  });

  // ------------------------------------------------------------------ resizable explorer
  const splitter = $("#splitter");
  const setWidth = (px) => document.documentElement.style.setProperty("--explorer-w", Math.max(180, Math.min(560, px)) + "px");
  setWidth(store.get(LS.width, 270));
  splitter.addEventListener("pointerdown", (e) => {
    splitter.setPointerCapture(e.pointerId);
    const move = (ev) => setWidth(ev.clientX - 6);
    const up = () => { splitter.removeEventListener("pointermove", move); splitter.removeEventListener("pointerup", up); store.set(LS.width, parseInt(getComputedStyle(document.documentElement).getPropertyValue("--explorer-w"), 10)); };
    splitter.addEventListener("pointermove", move); splitter.addEventListener("pointerup", up);
    document.querySelectorAll("iframe").forEach((f) => { f.style.pointerEvents = "none"; });
    splitter.addEventListener("pointerup", () => document.querySelectorAll("iframe").forEach((f) => { f.style.pointerEvents = ""; }), { once: true });
  });
  splitter.addEventListener("keydown", (e) => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--explorer-w"), 10) || 270;
    if (e.key === "ArrowLeft") setWidth(cur - 20); if (e.key === "ArrowRight") setWidth(cur + 20);
  });

  // ------------------------------------------------------------------ hooks for the wizards
  window.Shell = {
    refresh: loadExplorer, openTab,
    tables: () => tables, targets: () => targets, health: () => health,
    async reveal(tableId, system) {          // show a newly registered table in the explorer and select it
      await loadExplorer();
      ["root", "sources", "sys:" + system, "t" + tableId].forEach((id) => openSet.add(id));
      store.set(LS.open, [...openSet]); renderTree(); select("t" + tableId);
      const el = document.querySelector('#tree li[data-id="t' + tableId + '"] > .node'); if (el) el.scrollIntoView({ block: "center" });
    },
  };

  // ------------------------------------------------------------------ start
  (async () => {
    await loadExplorer();
    const saved = store.get(LS.tabs, null);
    const urls = (saved && saved.urls && saved.urls.length) ? saved.urls : ["/ui/start"];
    urls.forEach((u) => openTab(u));
    const keep = tabs.find((t) => saved && t.url === saved.active);
    activate(keep || tabs[0] || null);
    if (!tabs.length) renderTabs();
  })();
})();
