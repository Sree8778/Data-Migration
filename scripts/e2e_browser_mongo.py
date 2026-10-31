"""Edge/Playwright check of the MongoDB path of the Add Source wizard, against the Docker test server:

    docker compose -f docker-compose.servers.yml --profile mongo up -d --wait
    python scripts/e2e_browser_mongo.py [--base http://localhost:8000]

Seeds a collection of nested customer documents (1,200, some with missing fields, arrays and a field only the last one has), then in the
browser: choose MongoDB, a wrong password is rejected without echoing it, the right one connects, the database and its collections are
browsed, one collection is imported with its row count verified, and the explorer shows it. Then the server's own record is checked:
the dotted column names, the control total, that no password was stored. Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import sys

import httpx
from playwright.sync_api import sync_playwright
from pymongo import MongoClient

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--port", default="27018")
args = ap.parse_args()
BASE = args.base
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
api = httpx.Client(base_url=BASE, headers={"X-User": "mongo.tester"}, timeout=120)
NAME = "LEGACY_DOCS"
COLLECTION = "customers_docs"
api.delete(f"/api/connections/{NAME}")

root = MongoClient("localhost", int(args.port), username="root", password="Mongo_root_Test1", authSource="admin", serverSelectionTimeoutMS=5000)
db = root["legacy_docs"]
db.drop_collection(COLLECTION)
docs = [{"cust_id": f"{100000 + i}", "name": f"Kunde {i} GmbH", "country": ["DE", "AT", "CH", "NL"][i % 4],
         **({"address": {"street": f"Weg {i}", "city": f"Stadt {i % 40}", "geo": {"lat": 50 + i / 1000, "lon": 8 + i / 1000}}} if i % 7 else {}),
         **({"contacts": [{"name": "A", "mail": f"a{i}@example.com"}, {"name": "B", "mail": None}]} if i % 5 == 0 else {})} for i in range(1200)]
docs.append({"cust_id": "999999", "name": "Last One", "country": "DE", "legacy_flag": "X"})
db[COLLECTION].insert_many(docs)

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 940})
    page.add_init_script("localStorage.setItem('migration.user', 'mongo.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    nxt = page.locator("#wz-next")

    page.get_by_role("button", name="New Source").click()
    page.fill("#s-name", NAME)
    assert any("MongoDB" in k for k in page.locator("#s-kind option").all_inner_texts())
    page.select_option("#s-kind", "mongodb")
    assert "pymongo" in page.locator("#wz-body input[readonly]").input_value()
    nxt.click()
    assert page.locator('[data-opt="auth_source"]').count() == 1 and page.locator('[data-opt="tls"]').count() == 1 and page.locator("#c-port").input_value() == ""

    page.fill("#c-host", "localhost"); page.fill("#c-port", args.port); page.fill("#c-database", "legacy_docs"); page.fill("#c-user", "legacy_ro")
    page.fill('[data-opt="auth_source"]', "legacy_docs")
    page.fill("#c-password", "not-the-password")
    page.click("#c-test")
    page.locator("#c-status.bad").wait_for(timeout=30000)
    bad = page.locator("#c-status").inner_text()
    print("wrong password:", bad)
    assert "rejected" in bad and "not-the-password" not in bad and nxt.is_disabled()
    page.fill("#c-password", "Mongo_ro_Test1")
    page.click("#c-test")
    page.locator("#c-status.ok").wait_for(timeout=30000)
    print("test:", page.locator("#c-status").inner_text())
    nxt.click()

    page.locator("#d-tree .wz-schema").first.wait_for()
    schemas = [s.strip() for s in page.locator("#d-tree .wz-schema .hdr").all_inner_texts()]
    print("databases:", schemas)
    assert any("legacy_docs" in s for s in schemas) and not any(s.strip("+−− \n") in ("admin", "local", "config") for s in schemas)
    toggle = page.locator('[data-toggle="legacy_docs"]')
    if toggle.inner_text().strip() == "+":                      # a single database is opened for you
        toggle.click()
    page.locator(f'input[data-table="{COLLECTION}"]').wait_for()
    names = page.locator("#d-tree .tbl-name").all_inner_texts()
    print("collections:", names)
    assert COLLECTION in names
    page.locator(f'#d-tree .tbl-name[data-table="{COLLECTION}"]').click()
    page.locator("#d-cols", has_text="primary key").wait_for()
    cols = page.locator("#d-cols").inner_text()
    print("columns pane:", cols[:160].replace("\n", " "))
    assert "address.geo.lat" in cols and "_id" in cols
    page.locator(f'input[data-schema="legacy_docs"][data-table="{COLLECTION}"]').check()
    assert page.locator("#d-count").inner_text() == "1 selected"
    nxt.click()
    page.uncheck("#a-studio")
    nxt.click()
    summary = page.locator("#wz-body").inner_text()
    assert "legacy_ro" in summary and COLLECTION in summary and "Mongo_ro_Test1" not in summary
    page.screenshot(path="mock_data/wizard_mongo_summary.png")
    nxt.click()
    page.locator("#wizard[open]").wait_for(state="detached", timeout=120000)
    page.locator(f'#tree li[data-id="sys:{NAME}"] li[data-id^="t"] .label', has_text=COLLECTION).first.wait_for()
    print("imported; tabs:", page.locator("#tabstrip .tab .title").all_inner_texts())
    browser.close()

tables = {t["table_name"]: t for t in api.get("/api/tables").json() if t["system_name"] == NAME}
t = tables[COLLECTION]
print({k: (v["rows_landed"], v["system_type"]) for k, v in tables.items()})
assert t["rows_landed"] == 1201 and t["system_type"] == "MONGODB"
snap = api.get(f"/api/tables/{t['table_id']}/snapshot").json()
names = [c["name"] for c in snap["columns"]]
assert snap["control_status"] == "MATCH" and snap["expected_rows"] == 1201 and snap["origin"]["dialect"] == "mongodb" and snap["origin"]["user"] == "legacy_ro"
assert {"_id", "cust_id", "address.city", "address.geo.lat", "contacts", "legacy_flag"} <= set(names), names
assert "Mongo_ro_Test1" not in api.get("/api/connections").text
profile = api.get(f"/api/profile/{t['table_id']}").json()
assert profile["total_rows"] == 1201
print(f"profiled: {len(profile['columns'])} columns,", "no password stored")
db.drop_collection(COLLECTION)
print("console errors:", errors)
sys.exit(1 if errors else 0)
