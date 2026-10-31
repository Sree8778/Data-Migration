"""Edge/Playwright check of the Google Firebase (Firestore) path of the Add Source wizard, against the Firestore EMULATOR.

    docker compose -f docker-compose.servers.yml --profile firestore up -d --wait
    python scripts/e2e_browser_wizard_firestore.py [--base http://localhost:8000]

    # the same, against a REAL Firebase project (scratch project; writes 1,500 documents into a collection of its own and deletes them again):
    python scripts/e2e_browser_wizard_firestore.py --base http://localhost:8000 --key-file C:\\path\\key.json

Checks: Firebase is offered as a source and NOT as a target; the form asks for the project ID and the service-account key (the key box is masked, and loading a key file fills in the
project and the account); a damaged key is refused in plain words without repeating it; the emulator (localhost, no key) connects; the database and its collections are browsable;
a collection is imported with the row-count check; no key is stored anywhere. Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import httpx
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from playwright.sync_api import expect, sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--port", default="18080")
ap.add_argument("--key-file", default="", help="a service-account key file: test against the REAL project it belongs to instead of the emulator")
args = ap.parse_args()
REAL = bool(args.key_file)
BASE, NAME, COLLECTION = args.base, "FB_LEGACY", f"e2e_customers_{os.getpid()}" if REAL else "e2e_customers"
REAL_KEY = json.loads(Path(args.key_file).read_text(encoding="utf-8")) if REAL else {}
PROJECT = REAL_KEY.get("project_id", "demo-erp")
N = 1500 if REAL else 250                                      # the real service pages at 1,000: more than one page is the point
SECRET = REAL_KEY["private_key"].split("\n")[1][:40] if REAL else "NOT-A-REAL-KEY"   # a piece of the private key that must never show up anywhere
api = httpx.Client(base_url=BASE, headers={"X-User": "db.tester"}, timeout=120)
api.delete(f"/api/connections/{NAME}")

# ---------------------------------------------------------------- the data, written natively into the emulator
if REAL:
    from google.oauth2 import service_account
    db = firestore.Client(project=PROJECT, credentials=service_account.Credentials.from_service_account_info(REAL_KEY, scopes=["https://www.googleapis.com/auth/datastore"]))
else:
    os.environ["FIRESTORE_EMULATOR_HOST"] = f"localhost:{args.port}"
    db = firestore.Client(project=PROJECT, credentials=AnonymousCredentials())
    del os.environ["FIRESTORE_EMULATOR_HOST"]


def wipe():
    for doc in db.collection(COLLECTION).list_documents():
        doc.delete()


wipe()
batch = db.batch()
for i in range(N):
    batch.set(db.collection(COLLECTION).document(f"C{i:04d}"), {"name": f"Customer {i}", "city": f"City {i % 7}", "balance": i, "tags": ["a", "b"]})
    if i % 400 == 399:
        batch.commit()
        batch = db.batch()
batch.set(db.collection(COLLECTION).document("C9999"), {"name": "Late", "late_field": "only the last document has me"})
batch.commit()
print(f"{'REAL project ' + PROJECT if REAL else 'emulator'}: {N + 1} documents written to {COLLECTION}")

# a key file with the right shape and a private key that cannot be read: what a badly copied key looks like
fake_key = {"type": "service_account", "project_id": "my-real-project", "private_key_id": "x", "private_key": "-----BEGIN PRIVATE KEY-----\nNOT-A-REAL-KEY\n-----END PRIVATE KEY-----\n",
            "client_email": "reader@my-real-project.iam.gserviceaccount.com", "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}
key_path = Path(tempfile.mkdtemp()) / "key.json"
key_path.write_text(json.dumps(fake_key), encoding="utf-8")

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

    # ---------------------------------------------------------------- offered as a source, not as a target
    page.get_by_role("button", name="New Target").click()
    page.select_option("#g-type", "db")
    target_kinds = page.locator("#g-dialect option").all_inner_texts()
    print("target providers:", target_kinds)
    assert any("Firestore" in k for k in target_kinds) and any("PostgreSQL" in k for k in target_kinds) and not any("MongoDB" in k for k in target_kinds)   # Firebase can receive a delivery; MongoDB cannot
    page.click("#wz-cancel")
    page.wait_for_timeout(500)

    page.get_by_role("button", name="New Source").click()
    page.fill("#s-name", NAME)
    kinds = page.locator("#s-kind option").all_inner_texts()
    assert any("Firestore" in k for k in kinds), kinds
    page.select_option("#s-kind", "firestore")
    assert "google-cloud-firestore" in page.locator("#wz-body input[readonly]").input_value()
    nxt.click()

    # ---------------------------------------------------------------- the form
    assert page.locator("#c-database").count() == 1 and page.locator("#c-password").evaluate("e => e.tagName") == "TEXTAREA" and page.locator("#c-password").get_attribute("class") == "mask"
    assert page.locator("#c-host").input_value() == "" and page.locator('[data-opt="database_id"]').count() == 1
    assert nxt.is_disabled()
    page.click("#c-test")                                                    # nothing entered
    page.locator("#c-status.bad").wait_for()
    assert "service-account key" in page.locator("#c-status").inner_text()
    page.fill("#c-database", "my-real-project")
    page.click("#c-test")
    page.locator("#c-status.bad").wait_for()
    assert "service-account key" in page.locator("#c-status").inner_text()

    # a key file fills in the project and the account; the damaged private key is refused in plain words, without the key
    page.fill("#c-database", "")
    page.set_input_files("#c-keyinput", str(key_path))
    expect(page.locator("#c-database")).to_have_value("my-real-project")
    assert page.locator("#c-database").input_value() == "my-real-project" and "reader@my-real-project" in page.locator("#c-user").input_value()
    assert "NOT-A-REAL-KEY" in page.locator("#c-password").input_value()          # held in the page for the test, masked on screen
    page.click("#c-test")
    page.locator("#c-status.bad").wait_for()
    bad = page.locator("#c-status").inner_text()
    print("damaged key:", bad)
    assert "cannot be read" in bad and "NOT-A-REAL-KEY" not in bad and nxt.is_disabled()

    # not a key file at all
    bogus = key_path.with_name("bogus.json")
    bogus.write_text('{"type": "authorized_user"}', encoding="utf-8")
    page.set_input_files("#c-keyinput", str(bogus))
    expect(page.locator("#c-status")).to_contain_text("not a service-account key")

    if REAL:
        # ---------------------------------------------------------------- the real project: the real key file, loaded the way a person would
        page.fill("#c-database", ""); page.fill("#c-user", "")
        page.set_input_files("#c-keyinput", args.key_file)
        expect(page.locator("#c-database")).to_have_value(PROJECT)
        assert REAL_KEY["client_email"] in page.locator("#c-user").input_value() and page.locator("#c-host").input_value() == ""
        page.click("#c-test")
        page.locator("#c-status.ok").wait_for(timeout=60000)
        status = page.locator("#c-status").inner_text()
        print("test:", status)
        assert "Cloud Firestore" in status and "emulator" not in status and SECRET not in status
    else:
        # ---------------------------------------------------------------- the emulator: localhost, no key
        page.fill("#c-password", "")
        page.fill("#c-host", "localhost"); page.fill("#c-port", args.port); page.fill("#c-database", PROJECT)
        page.click("#c-test")
        page.locator("#c-status.ok").wait_for()
        print("test:", page.locator("#c-status").inner_text())
        assert "Cloud Firestore (emulator)" in page.locator("#c-status").inner_text()
    nxt.click()

    # ---------------------------------------------------------------- the database and its collections
    page.locator("#d-tree .wz-schema").first.wait_for()
    assert any("(default)" in s for s in page.locator("#d-tree .wz-schema .hdr").all_inner_texts())
    # a connection with one database opens it by itself: the collections are listed without a click
    page.locator(f'input[data-table="{COLLECTION}"]').wait_for()
    page.locator(f'#d-tree .tbl-name[data-table="{COLLECTION}"]').click()
    page.locator("#d-cols", has_text="primary key").wait_for()
    cols = page.locator("#d-cols").inner_text()
    print("columns pane:", cols[:140].replace("\n", " "))
    assert "_id" in cols and "balance" in cols
    page.locator(f'input[data-schema="(default)"][data-table="{COLLECTION}"]').check()
    others = page.locator(f'#d-tree input[type=checkbox]:checked:not([data-table="{COLLECTION}"])')      # the setup guide ticks every customer-looking table it suggests: keep only ours
    while others.count():
        others.first.uncheck()                                                                            # the list is redrawn after each change: look again every time
    assert page.locator("#d-count").inner_text() == "1 selected", (page.locator("#d-count").inner_text(), page.locator("#d-tree").inner_text()[:300])
    nxt.click()
    page.uncheck("#a-studio")
    nxt.click()
    summary = page.locator("#wz-body").inner_text()
    assert "Google Firebase (Firestore)" in summary and COLLECTION in summary and SECRET not in summary and "NOT-A-REAL-KEY" not in summary
    page.screenshot(path="mock_data/wizard_firestore_summary.png")
    nxt.click()
    page.locator("#wizard[open]").wait_for(state="detached", timeout=300000)
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for()
    browser.close()

# ---------------------------------------------------------------- what the server recorded
tables = {t["table_name"]: t for t in api.get("/api/tables").json() if t["system_name"] == NAME}
print({k: (v["rows_landed"], v["system_type"]) for k, v in tables.items()})
t = tables[COLLECTION]
assert t["rows_landed"] == N + 1 and t["system_type"] == "FIRESTORE"
snap = api.get(f"/api/tables/{t['table_id']}/snapshot").json()
assert snap["control_status"] == "MATCH" and snap["expected_rows"] == N + 1 and snap["origin"]["dialect"] == "firestore" and snap["origin"]["schema"] == "(default)"
names = [c["name"] for c in snap["columns"]]
assert names[0] == "_id" and "late_field" in names and "tags" in names
assert SECRET not in api.get("/api/connections").text and SECRET not in json.dumps(snap) and "NOT-A-REAL-KEY" not in api.get("/api/connections").text
profile = api.get(f"/api/profile/{t['table_id']}").json()
assert profile["total_rows"] == N + 1
wipe()
assert not list(db.collection(COLLECTION).list_documents()), "the scratch collection was not removed"
assert not errors, errors
print("OK: Firestore source wizard end to end")
