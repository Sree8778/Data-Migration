/* "API Key Stack" indicator in the top bar and its management dialog. Keys are never stored in the
   browser and are only ever displayed masked; the secret leaves this page only in the add/test request. */
(function () {
  "use strict";
  const { $, esc, api, run, table, badge } = App;
  const indicator = $("#key-indicator"), modal = $("#key-modal");
  if (!indicator || !modal) return;
  let pool = null, refreshTimer = null;

  function duration(seconds) {
    if (seconds >= 3600) return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m";
    if (seconds >= 60) return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
    return seconds + "s";
  }

  function statusBadge(k) {
    switch (k.status) {
      case "ACTIVE": return badge("green", "Active");
      case "RATE_LIMITED": return badge("amber", "Cooldown " + duration(k.seconds_until_available));
      case "QUOTA_EXHAUSTED": return badge("red", "Quota exceeded (resets in " + duration(k.seconds_until_available) + ")");
      default: return badge("red", "Invalid");
    }
  }

  function renderIndicator() {
    if (!pool) { indicator.className = "key-indicator gray"; $("#key-count").textContent = "n/a"; return; }
    const none = pool.total === 0;
    indicator.className = "key-indicator " + (none ? "gray" : pool.active > 0 ? "green" : "red");
    $("#key-count").textContent = none ? "none" : pool.active + " Active" + (pool.active < pool.total ? " / " + pool.total : "");
    indicator.title = "Gemini API key stack: " + pool.active + " active, " + pool.cooling_down + " cooling down, " +
      pool.quota_exhausted + " quota exceeded, " + pool.invalid + " invalid";
  }

  function renderModal() {
    if (!pool) return;
    $("#key-provider").textContent = "LLM provider: " + pool.provider + "  |  Gemini model: " + pool.model +
      "  |  Persistence: " + (pool.persisted ? "on" : "memory only");
    $("#key-list").innerHTML = table(["Key", "Label", "Status", "Requests", "Errors", "Last error", ""], pool.keys.map((k) => [
      "<code>" + esc(k.masked_key) + "</code>", esc(k.label || "-") + (k.source === "env" ? " " + badge("gray", "env") : ""),
      statusBadge(k), esc(k.request_count), esc(k.error_count), "<span class='muted'>" + esc(k.last_error || "-") + "</span>",
      '<button class="btn small test" data-hash="' + esc(k.key_hash) + '">Test</button> ' +
      '<button class="btn small del" data-hash="' + esc(k.key_hash) + '">Delete</button>']));
    modal.querySelectorAll(".del").forEach((b) => b.addEventListener("click", () => removeKey(b.dataset.hash)));
    modal.querySelectorAll(".test").forEach((b) => b.addEventListener("click", () => testPooled(b.dataset.hash)));
  }

  async function refresh() {
    try { pool = await api("GET", "/api/keys"); } catch (e) { pool = null; }
    renderIndicator(); renderModal();
  }

  function showResult(r) {
    $("#key-test-result").innerHTML = badge(r.ok ? "green" : "red", r.classification) + " " + esc(r.detail) + " (" + esc(r.latency_ms) + " ms)";
  }

  async function removeKey(hash) {
    if (!window.confirm("Remove this key from the stack?")) return;
    await run(async () => { await api("DELETE", "/api/keys/" + encodeURIComponent(hash)); App.toast("Key removed"); await refresh(); });
  }
  async function testPooled(hash) {
    await run(async () => { showResult(await api("POST", "/api/keys/test", { key_hash: hash })); await refresh(); });
  }

  $("#key-test").addEventListener("click", () => run(async () => {
    const key = $("#key-input").value.trim();
    if (!key) { App.toast("Paste a key first", true); return; }
    $("#key-test-result").textContent = "Testing...";
    showResult(await api("POST", "/api/keys/test", { api_key: key }));
  }));

  $("#key-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const k = await api("POST", "/api/keys", { api_key: $("#key-input").value.trim(), label: $("#key-label").value.trim() });
      $("#key-input").value = ""; $("#key-label").value = ""; $("#key-test-result").textContent = "";
      App.toast("Key " + k.masked_key + " added");
      await refresh();
    });
  });

  indicator.addEventListener("click", async () => {
    await refresh();
    if (typeof modal.showModal === "function") modal.showModal(); else modal.setAttribute("open", "");
    clearInterval(refreshTimer); refreshTimer = setInterval(refresh, 5000);   // live cooldown countdown
  });
  modal.addEventListener("close", () => {
    clearInterval(refreshTimer); $("#key-input").value = ""; $("#key-test-result").textContent = "";
  });

  refresh();
  setInterval(() => { if (!modal.open) refresh(); }, 60000);
})();
