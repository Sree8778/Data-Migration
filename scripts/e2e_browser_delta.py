"""Edge/Playwright check of DELTA runs: what changed since an earlier run, and merging only the changes into the table that run delivered.

    python scripts/create_demo_source_db.py        # once (creates legacy_demo)
    python scripts/e2e_browser_delta.py [--base http://localhost:8000]

1. Through the API: a clean approved run A, signed off and delivered into PostgreSQL (legacy_demo, schema staging).
2. The landed source then changes (4 customers move city, 2 disappear, 3 are new) and run B is made and signed off.
3. In the Governance hub of run B: compares with run A (new / changed / unchanged / removed), merges the delta into the table with 'delete records
   that are no longer valid' ticked, checks in PostgreSQL itself what the table holds, then rolls the merge back and checks that the table is
   exactly what run A left. Finally run A's delivery is rolled back too (this tidies the demo database).
Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

import duckdb
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
AUTHOR, STEWARD, TARGET, TABLE = "delta.author", "delta.steward", "DELTA_PG", "MIG_DELTA_E2E"


def as_user(user):
    return httpx.Client(base_url=BASE, headers={"X-User": user}, timeout=600)


author, steward = as_user(AUTHOR), as_user(STEWARD)
author.delete(f"/api/connections/{TARGET}")
admin = psycopg2.connect(host="localhost", port=int(args.port), dbname="legacy_demo", user="migration", password="migration_dev_pw")
admin.autocommit = True
admin.cursor().execute(f"CREATE SCHEMA IF NOT EXISTS staging; DROP TABLE IF EXISTS staging.{TABLE.lower()}")

# ------------------------------------------------------------------ 1. run A: clean, approved, signed off, delivered
rows = list(csv.DictReader(open(ROOT / "mock_data" / "legacy_ar_customers.csv", newline="", encoding="utf-8")))
clean = [r for r in rows if r["CITY"].strip() and r["COUNTRY"].strip().upper() in COUNTRY_SEED and len(r["CUST_NAME"].strip()) <= 40
         and (not r["PAYMENT_TERMS"].strip() or r["PAYMENT_TERMS"].strip().upper() in PAYMENT_TERMS_SEED)][:300]
buf = io.StringIO()
w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\r\n")
w.writeheader(); w.writerows(clean)
author.post("/api/admin/seed-sap-catalog")
import time  # noqa: E402
reg = author.post("/api/sources/register", data={"system_name": f"E2E_DELTA_{time.strftime('%H%M%S')}", "table_name": "AR_CLEAN", "primary_key_columns": "CUST_ID"},
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
run_a = author.post(f"/api/pipeline/run/{mset}", json={"wave_name": "DELTA_A"}).json()
assert run_a["readiness"]["state"] == "READY_FOR_LOAD", run_a["readiness"]
a_id, valid_a = run_a["run_id"], run_a["validation"]["valid_records"]
steward.post(f"/api/governance/sign-off-load/{a_id}").raise_for_status()
author.post("/api/connections", json={"name": TARGET, "dialect": "postgresql", "host": "localhost", "port": int(args.port), "database": "legacy_demo", "user": "migration",
                                     "password": "migration_dev_pw", "role": "target", "default_schema": "staging"}).raise_for_status()
steward.post(f"/api/pipeline/load-db/{a_id}", json={"connection": TARGET, "table_name": TABLE}).raise_for_status()
print(f"run {a_id}: {valid_a} records delivered into staging.{TABLE.lower()}")

# ------------------------------------------------------------------ 2. the source changes; run B
landed = next((ROOT / "workspace" / "tables" / str(table_id)).glob("source.*"))
con = duckdb.connect()
con.execute(f"CREATE TABLE src AS SELECT * FROM read_parquet('{landed.as_posix()}')")
ids = [r[0] for r in con.execute("SELECT CUST_ID FROM src ORDER BY CAST(CUST_ID AS BIGINT)").fetchall()]
valid_ids = {r[0] for r in duckdb.connect().execute(f"SELECT CAST(_SRC_PK AS VARCHAR) FROM read_parquet('{run_a['validation']['valid_path']}')").fetchall()}
pick = [i for i in ids if i in valid_ids]
moved, gone = pick[0:4], pick[4:6]
quote = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
con.execute(f"UPDATE src SET CITY = 'Changedville' WHERE CUST_ID IN ({quote(moved)})")
con.execute(f"DELETE FROM src WHERE CUST_ID IN ({quote(gone)})")
for n, (name, street, city) in enumerate([("Zyxwv Quokka GmbH", "Hafenstrasse 1", "Kiel"), ("Plimth Orchards Ltd", "Orchard Lane 2", "Leeds"), ("Vandelay Imports SA", "Rue Neuve 3", "Lyon")]):
    con.execute(f"INSERT INTO src SELECT * REPLACE ('{9000001 + n}' AS CUST_ID, '{name}' AS CUST_NAME, '' AS TAX_ID, '' AS EMAIL, '' AS PHONE, '{street}' AS ADDR_STREET, '{city}' AS CITY) "
                f"FROM src WHERE CUST_ID = '{pick[20]}'")
con.execute(f"COPY src TO '{landed.as_posix()}' (FORMAT parquet)")
run_b = author.post(f"/api/pipeline/run/{mset}", json={"wave_name": "DELTA_B"}).json()
assert run_b["readiness"]["state"] == "READY_FOR_LOAD", run_b["readiness"]
b_id = run_b["run_id"]
steward.post(f"/api/governance/sign-off-load/{b_id}").raise_for_status()
print(f"run {b_id}: {run_b['validation']['valid_records']} valid records; the source changed: {len(moved)} moved, {len(gone)} gone, 3 new")


def table_dump():
    cur = admin.cursor()
    cur.execute(f"SELECT * FROM staging.{TABLE.lower()} ORDER BY legacy_key")
    return cur.fetchall()


before_merge = table_dump()
assert len(before_merge) == valid_a

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.add_init_script(f"localStorage.setItem('migration.user', '{STEWARD}')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept("the delta was a test"))
    page.goto(f"{BASE}/ui/governance/{b_id}")

    # ---- 3a. compare with run A
    page.locator("#delta-baseline option").first.wait_for(state="attached")
    assert page.locator("#delta-baseline").input_value() == str(a_id)
    page.click("#delta-compare")
    page.locator("#delta-result .stat").first.wait_for()
    cards = {c.locator(".label").inner_text().title(): c.locator(".value").inner_text() for c in page.locator("#delta-result .stat").all()}
    print("delta:", cards)
    assert cards == {"New": "3", "Changed": "4", "Unchanged": str(valid_a - 4 - 2), "Removed": "2"}, cards
    assert "not in current valid 2" in page.locator("#delta-result").inner_text()

    # ---- 3b. merge the changes (and delete the two that left) into run A's table
    page.fill("#db-table", TABLE)
    page.select_option("#db-mode", "merge")
    page.locator("#db-delete-wrap:not(.hidden)").wait_for()
    page.check("#db-delete")
    page.click("#db-load")
    page.locator("#toast.show", has_text="delivered").wait_for()
    page.locator("#db-history", has_text="3 new, 4 replaced, 2 deleted").wait_for()
    assert "adds up" in page.locator("#db-history").inner_text() and "cells checked" in page.locator("#db-history").inner_text()
    page.screenshot(path="mock_data/governance_delta_merge.png")
    after = table_dump()
    assert len(after) == valid_a + 3 - 2, (len(after), valid_a)
    cur = admin.cursor()
    cur.execute(f"SELECT count(*) FROM staging.{TABLE.lower()} WHERE adrc_city1 = 'Changedville'")
    assert cur.fetchone()[0] == 4
    cur.execute(f"SELECT count(*) FROM staging.{TABLE.lower()} WHERE legacy_key IN ({quote(gone)})")
    assert cur.fetchone()[0] == 0
    print(f"table now holds {len(after)} rows: {valid_a} + 3 new - 2 removed, 4 cities changed")

    # ---- 3c. the merge is rolled back: the table is exactly what run A left
    page.locator("#db-history .db-rollback").first.click()
    page.locator("#toast.show", has_text="written by this merge were removed").wait_for()
    page.locator("#db-history", has_text="rolled back by").wait_for()
    print("rollback:", page.locator("#toast").inner_text())
    assert table_dump() == before_merge
    page.screenshot(path="mock_data/governance_delta_rolled_back.png")
    browser.close()

checks = {c["name"]: c for c in steward.get(f"/api/reconciliation/{b_id}").json()["report"]["checks"]}
assert checks["db_rollback_postgresql_staging.mig_delta_e2e"]["passed"], checks
print("certificate:", checks["db_rollback_postgresql_staging.mig_delta_e2e"]["detail"][:160])

# ---- tidy up: undo run A's delivery too (its table is then empty and dropped)
steward.post(f"/api/pipeline/rollback-db/{a_id}", json={"connection": TARGET, "table_name": TABLE, "reason": "tidy up the e2e check"}).raise_for_status()
cur = admin.cursor()
cur.execute(f"SELECT count(*) FROM pg_tables WHERE schemaname = 'staging' AND tablename = '{TABLE.lower()}'")
assert cur.fetchone()[0] == 0
author.delete(f"/api/connections/{TARGET}")
admin.close()
unexpected = [e for e in errors if "422" not in e]
print("console errors:", unexpected)
sys.exit(1 if unexpected else 0)
