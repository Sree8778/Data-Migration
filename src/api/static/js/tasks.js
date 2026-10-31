(function () {
  "use strict";
  const { $, esc, api, run, table, num, badge, stat } = App;
  let owner = "";

  const STATUS_KIND = { OPEN: "amber", IN_PROGRESS: "amber", DONE: "green", WONT_FIX: "gray" };
  const PRIORITY_KIND = { HIGH: "red", MEDIUM: "amber", LOW: "gray" };

  function render(list, sum) {
    $("#summary").innerHTML = [stat("Open", num(sum.open)), stat("Overdue", num(sum.overdue)), stat("Unassigned", num(sum.unassigned)), stat("Done", num(sum.by_status.DONE || 0))].join("") +
      Object.entries(sum.by_owner).map(([o, v]) => stat(o, v.open + " open" + (v.overdue ? ", " + v.overdue + " overdue" : ""))).join("");
    $("#f-info").textContent = num(list.total) + " task(s)" + (owner ? " for " + owner : "");
    $("#list").innerHTML = table(["#", "Task", "Owner", "Due", "Priority", "Status", "Last note", ""], list.tasks.map((t) => {
      const last = t.notes.length ? t.notes[t.notes.length - 1] : null;
      return [num(t.id), "<strong>" + esc(t.title) + "</strong><br><span class='muted'>" + esc(t.kind) + (t.ref.run_id ? " - run " + esc(t.ref.run_id) : "") + "</span>",
        "<input class='task-owner' data-id='" + esc(t.id) + "' value='" + esc(t.owner || "") + "' placeholder='unassigned' maxlength='128'>",
        esc(t.due || "-") + (t.overdue ? " " + badge("red", "overdue") : ""), badge(PRIORITY_KIND[t.priority], t.priority), badge(STATUS_KIND[t.status], t.status),
        last ? "<span class='muted'>" + esc(last.by) + ": " + esc(last.text || last.changes.join("; ")) + "</span>" : "",
        "<button type='button' class='btn small' data-status='IN_PROGRESS' data-id='" + esc(t.id) + "'>Start</button> <button type='button' class='btn small' data-status='DONE' data-id='" + esc(t.id) + "'>Done</button> " +
        "<button type='button' class='btn small' data-status='WONT_FIX' data-id='" + esc(t.id) + "'>Won't fix</button>"];
    }));
  }

  async function load() {
    const q = new URLSearchParams();
    if (owner) q.set("owner", owner);
    if ($("#f-open").checked) q.set("open_only", "true");
    render(await api("GET", "/api/tasks?" + q.toString()), await api("GET", "/api/tasks/summary"));
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-status]");
    if (!b) return;
    run(async () => { await api("POST", "/api/tasks/" + b.dataset.id + "/update", { status: b.dataset.status }); await load(); });
  });
  document.addEventListener("change", (e) => {
    if (!e.target.classList.contains("task-owner")) return;
    run(async () => { await api("POST", "/api/tasks/" + e.target.dataset.id + "/update", { owner: e.target.value.trim() }); App.toast("Owner changed."); await load(); });
  });
  $("#t-create").addEventListener("click", () => run(async () => {
    await api("POST", "/api/tasks", { title: $("#t-title").value, owner: $("#t-owner").value.trim() || null, due: $("#t-due").value || null, priority: $("#t-priority").value });
    $("#t-title").value = ""; App.toast("Task created."); await load();
  }));
  function routes() {
    const out = {};
    $("#d-routes").value.split(",").forEach((p) => { const [k, v] = p.split("="); if (k && v) out[k.trim().toUpperCase()] = v.trim(); });
    return out;
  }
  $("#d-create").addEventListener("click", () => run(async () => {
    const id = Number($("#d-run").value);
    if (!id) { App.toast("Enter a run number.", true); return; }
    const r = await api("POST", "/api/tasks/from-defects/" + id, { owner: $("#d-owner").value.trim() || null, due: $("#d-due").value || null, owners_by_table: routes() });
    App.toast(r.created + " task(s) created."); await load();
  }));
  $("#d-sync").addEventListener("click", () => run(async () => {
    const id = Number($("#d-run").value);
    if (!id) { App.toast("Enter a run number.", true); return; }
    const r = await api("POST", "/api/tasks/sync/" + id);
    App.toast(r.closed + " task(s) closed."); await load();
  }));
  $("#f-mine").addEventListener("click", () => { owner = App.user(); run(load); });
  $("#f-all").addEventListener("click", () => { owner = ""; run(load); });
  $("#f-open").addEventListener("change", () => run(load));
  run(load);
})();
