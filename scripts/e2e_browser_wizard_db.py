"""Edge/Playwright check of the database path of the Add Source wizard, against the demo legacy database.

    python scripts/create_demo_source_db.py        # once
    python scripts/e2e_browser_wizard_db.py [--base http://localhost:8000]

Connects as the read-only login to legacy_demo, browses schemas and tables, imports three tables (one with a composite key, one
code table, 1,980 customers) with row-count verification, and checks the explorer, the profile, that no password was stored, and
that cancelling the wizard removes the connection it saved. Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import sys

import httpx
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--port", default="5433")
args = ap.parse_args()
BASE = args.base
api = httpx.Client(base_url=BASE, headers={"X-User": "db.tester"}, timeout=120)
NAME = "LEGACY_ERP_PG"
api.delete(f"/api/connections/{NAME}")

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 940})
    page.add_init_script("localStorage.setItem('migration.user', 'db.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    nxt = page.locator("#wz-next")

    # ---------------------------------------------------------------- provider
    page.get_by_role("button", name="New Source").click()
    page.fill("#s-name", NAME)
    kinds = page.locator("#s-kind option").all_inner_texts()
    print("providers:", kinds)
    assert any("PostgreSQL" in k for k in kinds) and any("Oracle" in k for k in kinds) and not page.locator("#s-kind option[disabled]").count()
    # the Azure presets fill in the encryption those services demand
    page.select_option("#s-kind", "preset:azure-sql")
    assert "windows.net" in page.locator("#wz-body .hint").last.inner_text()
    nxt.click()
    assert page.locator('[data-opt="encrypt"]').is_checked() and page.locator("#c-port").input_value() == "1433"
    page.locator("#wz-back").click()
    page.select_option("#s-kind", "preset:azure-postgresql")
    nxt.click()
    assert page.locator('[data-opt="sslmode"]').input_value() == "require" and page.locator("#c-port").input_value() == "5432"
    page.locator("#wz-back").click()
    page.select_option("#s-kind", "postgresql")
    assert "psycopg2" in page.locator("#wz-body input[readonly]").input_value()
    nxt.click()
    assert not page.locator('[data-opt="sslmode"]').input_value() and page.locator("#c-port").input_value() == ""   # a plain choice clears the preset

    # ---------------------------------------------------------------- connection: wrong password first
    assert nxt.is_disabled()
    page.fill("#c-host", "localhost"); page.fill("#c-port", args.port); page.fill("#c-database", "legacy_demo"); page.fill("#c-user", "legacy_ro")
    page.fill("#c-password", "not-the-password")
    page.click("#c-test")
    page.locator("#c-status.bad").wait_for()
    bad = page.locator("#c-status").inner_text()
    print("wrong password:", bad)
    assert "rejected" in bad and "not-the-password" not in bad and nxt.is_disabled()
    page.fill("#c-password", "legacy_ro_pw")
    page.click("#c-test")
    page.locator("#c-status.ok").wait_for()
    print("test:", page.locator("#c-status").inner_text())
    assert "schema(s) visible" in page.locator("#c-status").inner_text()
    nxt.click()

    # ---------------------------------------------------------------- tables
    page.locator("#d-tree .wz-schema").first.wait_for()
    schemas = page.locator("#d-tree .wz-schema .hdr").all_inner_texts()
    print("schemas:", [s.strip() for s in schemas])
    assert any("ar" == s.strip("+−− \n") or "ar" in s for s in schemas) and not any("pg_catalog" in s for s in schemas)   # system schemas hidden
    assert nxt.is_disabled()
    page.locator('[data-toggle="ar"]').click()
    page.locator('input[data-table="customers"]').wait_for()
    names = page.locator("#d-tree .tbl-name").all_inner_texts()
    print("ar tables:", names)
    assert {"customers", "customer_contacts", "payment_terms", "v_open_customers"} <= set(names)
    page.fill("#d-filter", "pay")
    page.locator("#d-tree .tbl-name", has_text="payment_terms").wait_for()
    page.locator('#d-tree .tbl-name[data-table="customers"]').wait_for(state="detached")
    page.locator('input[data-table="payment_terms"]').check()
    page.fill("#d-filter", "")
    page.locator('#d-tree .tbl-name[data-table="customers"]').wait_for()
    page.locator('#d-tree .tbl-name[data-table="customers"]').click()
    page.locator("#d-cols", has_text="primary key").wait_for()
    print("columns pane:", page.locator("#d-cols").inner_text()[:120].replace("\n", " "))
    page.locator('input[data-schema="ar"][data-table="customers"]').check()
    page.locator('[data-toggle="sales"]').click()
    page.locator('input[data-table="order_lines"]').check()
    assert page.locator("#d-count").inner_text() == "3 selected"
    nxt.click()
    page.uncheck("#a-studio")
    nxt.click()
    summary = page.locator("#wz-body").inner_text()
    assert "3 selected" in summary or "3 tables" in summary or "Tables" in summary
    assert "legacy_ro" in summary and "ar.customers" in summary and "sales.order_lines" in summary and "legacy_ro_pw" not in summary
    page.screenshot(path="mock_data/wizard_db_summary.png")
    assert nxt.inner_text() == "Finish"
    nxt.click()
    page.locator("#wizard[open]").wait_for(state="detached", timeout=120000)
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for()
    page.locator(f'#tree li[data-id="sys:{NAME}"] li[data-id^="t"] .label', has_text="customers").first.wait_for()   # this source's own table, whatever other systems hold
    print("imported; tabs:", page.locator("#tabstrip .tab .title").all_inner_texts())

    # ---------------------------------------------------------------- cancel removes a connection saved only for browsing
    page.get_by_role("button", name="New Source").click()
    page.fill("#s-name", "CANCEL_ME"); page.select_option("#s-kind", "postgresql"); nxt.click()
    page.fill("#c-host", "localhost"); page.fill("#c-port", args.port); page.fill("#c-database", "legacy_demo"); page.fill("#c-user", "legacy_ro"); page.fill("#c-password", "legacy_ro_pw")
    page.click("#c-test"); page.locator("#c-status.ok").wait_for()
    assert any(c["name"] == "CANCEL_ME" for c in api.get("/api/connections").json())
    page.click("#wz-cancel")
    page.wait_for_timeout(800)
    assert not any(c["name"] == "CANCEL_ME" for c in api.get("/api/connections").json())
    print("cancel removed the temporary connection")
    browser.close()

# ---------------------------------------------------------------- what the server recorded
tables = {t["table_name"]: t for t in api.get("/api/tables").json() if t["system_name"] == NAME}
print({k: (v["rows_landed"], v["system_type"]) for k, v in tables.items()})
assert set(tables) == {"customers", "payment_terms", "order_lines"}
assert tables["customers"]["rows_landed"] == 1980 and tables["customers"]["system_type"] == "POSTGRESQL"
snap = api.get(f"/api/tables/{tables['customers']['table_id']}/snapshot").json()
assert snap["control_status"] == "MATCH" and snap["expected_rows"] == 1980 and snap["origin"]["schema"] == "ar" and snap["origin"]["user"] == "legacy_ro"
conns = api.get("/api/connections").json()
assert [c["name"] for c in conns if c["name"] == NAME] == [NAME] and conns[0]["password_source"] in ("memory", "missing")
assert "legacy_ro_pw" not in api.get("/api/connections").text
profile = api.get(f"/api/profile/{tables['customers']['table_id']}").json()
assert profile["total_rows"] == 1980
keys = {t["table_name"]: [c["column_name"] for c in api.get(f"/api/profile/{t['table_id']}").json()["columns"]] for t in tables.values()}
print("profiled:", {k: len(v) for k, v in keys.items()})

unexpected = [e for e in errors if "422" not in e and "502" not in e]
print("console errors (excluding the expected 422/502 from the wrong-password test):", unexpected)
sys.exit(1 if unexpected else 0)
