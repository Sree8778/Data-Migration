"""Edge/Playwright check of DATABASE TARGETS: the Add Target wizard (database path) and delivering a signed-off run from the Governance hub.

    python scripts/create_demo_source_db.py        # once (creates legacy_demo)
    python scripts/e2e_browser_targets.py [--base http://localhost:8000]

1. Builds a clean, approved, signed-off run through the API (clean rows of the mock extract; author, approver and sign-off are
   different users, as the gate demands).
2. In the browser: New Target -> Database -> PostgreSQL, connects with the dev admin login to legacy_demo, picks schema `staging`,
   proves write access, saves the target.
3. In the Governance hub: previews the CREATE TABLE statement, delivers the run, then checks in PostgreSQL itself that the table holds
   exactly the validated records, and that the reconciliation certificate has a passing delivery check.
Exit 1 on any failed assertion or unexpected console error.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

import httpx
import psycopg2
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.services.lookup_service import COUNTRY_SEED, PAYMENT_TERMS_SEED  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--port", default="5433")
args = ap.parse_args()
BASE = args.base
AUTHOR, STEWARD = "e2e.author", "e2e.steward"
TARGET = "STAGING_PG"


def as_user(user):
    return httpx.Client(base_url=BASE, headers={"X-User": user}, timeout=600)


author, steward = as_user(AUTHOR), as_user(STEWARD)
author.delete(f"/api/connections/{TARGET}")

# ------------------------------------------------------------------ 1. a clean, approved, signed-off run
rows = list(csv.DictReader(open(ROOT / "mock_data" / "legacy_ar_customers.csv", newline="", encoding="utf-8")))
clean = [r for r in rows if r["CITY"].strip() and r["COUNTRY"].strip().upper() in COUNTRY_SEED and len(r["CUST_NAME"].strip()) <= 40
         and (not r["PAYMENT_TERMS"].strip() or r["PAYMENT_TERMS"].strip().upper() in PAYMENT_TERMS_SEED)][:400]
buf = io.StringIO()
w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\r\n")
w.writeheader(); w.writerows(clean)
author.post("/api/admin/seed-sap-catalog")
reg = author.post("/api/sources/register", data={"system_name": "E2E_TARGETS", "table_name": "AR_CLEAN", "primary_key_columns": "CUST_ID"},
                  files={"file": ("ar_clean.csv", buf.getvalue().encode("utf-8"))})
assert reg.status_code == 201, reg.text
table_id = reg.json()["table_id"]
author.post(f"/api/profile/{table_id}").raise_for_status()
mset = author.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
for tt, tc, rt, src, logic, lookups in [
        ("BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME", None, None), ("ADRC", "STREET", "DIRECT_COPY", "ADDR_STREET", None, None), ("ADRC", "CITY1", "DIRECT_COPY", "CITY", None, None),
        ("ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", None, COUNTRY_SEED), ("ADRC", "POST_CODE1", "DIRECT_COPY", "POSTAL_CODE", None, None),
        ("KNB1", "ZTERM", "VALUE_MAPPING", "PAYMENT_TERMS", None, PAYMENT_TERMS_SEED), ("BUT000", "BU_GROUP", "STATIC_VALUE", None, "'BP01'", None), ("BUT000", "TYPE", "STATIC_VALUE", None, "'2'", None)]:
    author.post(f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": rt, "source_column": src, "transformation_logic": logic, "lookups": lookups}).raise_for_status()
author.post(f"/api/governance/submit-mapping/{mset}").raise_for_status()
steward.post(f"/api/governance/approve-mapping/{mset}").raise_for_status()
run = author.post(f"/api/pipeline/run/{mset}", json={"wave_name": "E2E_DB_TARGET"}).json()
run_id, valid = run["run_id"], run["validation"]["valid_records"]
assert run["readiness"]["state"] == "READY_FOR_LOAD", run["readiness"]
steward.post(f"/api/governance/sign-off-load/{run_id}").raise_for_status()
print(f"run {run_id}: {valid} validated records, signed off, gate READY_FOR_LOAD")

# the target schema (the wizard's job is to prove we may write there, not to create it)
admin = psycopg2.connect(host="localhost", port=int(args.port), dbname="legacy_demo", user="migration", password="migration_dev_pw")
admin.autocommit = True
admin.cursor().execute("CREATE SCHEMA IF NOT EXISTS staging; DROP TABLE IF EXISTS staging.mig_bp_e2e")   # start clean even after an aborted run

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 940})
    page.add_init_script(f"localStorage.setItem('migration.user', '{STEWARD}')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    nxt = page.locator("#wz-next")

    # ------------------------------------------------------------ 2. the Add Target wizard, database path
    page.get_by_role("button", name="New Target").click()
    page.select_option("#g-type", "db")
    page.fill("#g-name", TARGET)
    kinds = page.locator("#g-dialect option").all_inner_texts()
    print("database providers:", kinds)
    assert {"PostgreSQL", "Oracle Database", "Microsoft SQL Server", "MySQL / MariaDB"} <= set(kinds)
    nxt.click()
    assert nxt.is_disabled()
    page.fill("#c-host", "localhost"); page.fill("#c-port", args.port); page.fill("#c-database", "legacy_demo"); page.fill("#c-user", "migration"); page.fill("#c-password", "migration_dev_pw")
    page.click("#c-test"); page.locator("#c-status.ok").wait_for()
    nxt.click()
    page.locator("#t-schema option", has_text="staging").wait_for(state="attached")
    assert nxt.is_disabled()                                   # write access must be proven first
    page.select_option("#t-schema", "staging")
    page.click("#t-test")
    page.locator("#t-status.ok").wait_for()
    print("write test:", page.locator("#t-status").inner_text())
    nxt.click(); nxt.click()
    summary = page.locator("#wz-body").inner_text()
    assert TARGET in summary and "staging" in summary and "migration_dev_pw" not in summary
    page.screenshot(path="mock_data/wizard_target_db_summary.png")
    nxt.click()
    page.locator("#wizard[open]").wait_for(state="detached")
    page.locator("#toast.show", has_text="saved").wait_for()
    page.locator('#tree li[data-id="targets"] > .node > .twist').click()
    page.locator(f'#tree li[data-id="targetdb-{TARGET}"]').wait_for()
    print("explorer lists the new target")

    # ------------------------------------------------------------ 3. deliver from the Governance hub
    page.evaluate(f"""localStorage.setItem('migration.console.tabs', JSON.stringify({{urls: ['/ui/governance/{run_id}'], active: '/ui/governance/{run_id}'}}))""")
    page.reload()
    frame = page.frame_locator("iframe.active")
    frame.locator("#db-conn option", has_text=TARGET).wait_for(state="attached")
    assert frame.locator("#db-schema").get_attribute("placeholder") == "connection default"
    frame.locator("#db-table").fill("MIG_BP_E2E")
    frame.locator("#db-preview").click()
    frame.locator("#db-ddl:not(.hidden)").wait_for()
    ddl = frame.locator("#db-ddl").inner_text()
    print(ddl.splitlines()[0], "|", ddl.splitlines()[1])
    assert 'CREATE TABLE "staging"."mig_bp_e2e"' in ddl and 'PRIMARY KEY ("legacy_key")' in ddl and f"{valid} row(s)" in ddl
    cur = admin.cursor()
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'staging' AND tablename = 'mig_bp_e2e'")
    assert cur.fetchone()[0] == 0                                # the preview created nothing
    frame.locator("#db-load").click()
    frame.locator("#toast.show").wait_for()                     # the page's own toast, inside its tab
    print("toast:", frame.locator("#toast").inner_text())
    assert "delivered" in frame.locator("#toast").inner_text(), frame.locator("#toast").inner_text()
    frame.locator("#db-history table").wait_for()
    assert "matches" in frame.locator("#db-history").inner_text()
    assert "cells checked" in frame.locator("#db-history").inner_text()      # every value was read back before the commit
    page.screenshot(path="mock_data/governance_db_delivery.png")

    # ------------------------------------------------------------ 3b. the value comparison, now and after someone edits the table
    frame.locator("#toast").evaluate("el => { el.className = ''; }")
    frame.locator("#db-history .db-verify").click()
    frame.locator("#toast.show", has_text="identical to the validated payload").wait_for()
    frame.locator("#db-history", has_text="still identical").wait_for()
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("UPDATE staging.mig_bp_e2e SET but000_name_org1 = 'Edited by hand' WHERE legacy_key = (SELECT min(legacy_key) FROM staging.mig_bp_e2e)")
    frame.locator("#toast").evaluate("el => { el.className = ''; }")
    frame.locator("#db-history .db-verify").click()
    frame.locator("#toast.show", has_text="The table has changed: but000_name_org1").wait_for()
    frame.locator("#db-history", has_text="changed: but000_name_org1").wait_for()
    print("value comparison:", frame.locator("#toast").inner_text())
    page.screenshot(path="mock_data/governance_db_values.png")
    browser.close()

# ------------------------------------------------------------------ the database itself is the witness
cur = admin.cursor()
cur.execute("SELECT count(*), count(DISTINCT legacy_key), min(mig_run_id), max(mig_run_id) FROM staging.mig_bp_e2e")
count, distinct, lo, hi = cur.fetchone()
print("in PostgreSQL:", count, "rows,", distinct, "distinct keys, run", lo)
assert count == distinct == valid and lo == hi == run_id
cur.execute("SELECT adrc_country, count(*) FROM staging.mig_bp_e2e GROUP BY 1 ORDER BY 2 DESC LIMIT 3")
print("countries delivered as SAP keys:", cur.fetchall())
checks = {c["name"]: c for c in steward.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}
delivery = [c for n, c in checks.items() if n.startswith("db_delivery_")]
assert len(delivery) == 1 and delivery[0]["passed"], delivery
print("certificate:", delivery[0]["detail"][:150])

# ------------------------------------------------------------------ the delivery is undone from the Governance hub (this also tidies the demo database)
with sync_playwright() as p2:
    browser2 = p2.chromium.launch(channel="msedge", headless=True)
    page2 = browser2.new_page(viewport={"width": 1400, "height": 940})
    page2.add_init_script(f"localStorage.setItem('migration.user', '{STEWARD}')")
    page2.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page2.on("pageerror", lambda e: errors.append(str(e)))
    page2.on("dialog", lambda d: d.accept("the demo is over, undo this delivery"))
    page2.goto(f"{BASE}/ui/governance/{run_id}")
    page2.locator("#db-history .db-rollback").click()
    page2.locator("#toast.show", has_text="removed from").wait_for()
    page2.locator("#db-history", has_text="rolled back by").wait_for()
    print("rollback:", page2.locator("#toast").inner_text())
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'staging' AND tablename = 'mig_bp_e2e'")
    assert cur.fetchone()[0] == 0                                   # emptied, so the table went with its rows
    checks = {c["name"]: c for c in steward.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}
    rolled = [c for n, c in checks.items() if n.startswith("db_rollback_")]
    assert len(rolled) == 1 and rolled[0]["passed"] and "the demo is over" in rolled[0]["detail"], rolled
    print("certificate:", rolled[0]["detail"][:170])
    browser2.close()
admin.close()

unexpected = [e for e in errors if "422" not in e]
print("console errors:", unexpected)
sys.exit(1 if unexpected else 0)
