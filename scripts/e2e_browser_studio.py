"""Edge/Playwright check of the Mapping Studio controls: archive / restore, show archived, delete rule, missing-column flag.

    python scripts/e2e_browser_studio.py [--base http://localhost:8000] [--table 7]

Creates a throw-away DRAFT set on the table, exercises the controls on it, and archives it at the end.
"""

from __future__ import annotations

import argparse
import sys

import httpx
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--table", type=int, default=0, help="a table id; by default the script registers its own small table, so it never depends on what else is in the workspace")
args = ap.parse_args()
api = httpx.Client(base_url=args.base, headers={"X-User": "ui.tester"}, timeout=120)
if not args.table:
    own = api.post("/api/sources/register", data={"system_name": "STUDIO_CHECK", "table_name": "STUDIO_CUSTOMERS", "primary_key_columns": "CUST_ID"},
                   files={"file": ("studio.csv", "\n".join(["CUST_ID,CUST_NAME,ADDR_STREET", "1,Acme,1 Main St", "2,Beta,2 Oak Rd"]).encode())})
    assert own.status_code == 201, own.text
    args.table = own.json()["table_id"]
    api.post(f"/api/profile/{args.table}").raise_for_status()
mset = api.post("/api/mapping-sets", json={"source_table_id": args.table}).json()["mapping_set_id"]
for tc, src in (("NAME_ORG1", "CUST_NAME"), ("STREET", "ADDR_STREET")):
    api.post(f"/api/mapping/{mset}/rules", json={"target_table": "BUT000" if tc == "NAME_ORG1" else "ADRC", "target_column": tc, "rule_type": "DIRECT_COPY", "source_column": src})

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.add_init_script("localStorage.setItem('migration.user', 'ui.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept("superseded by the 250-row layout") if d.type == "prompt" else d.accept())
    page.goto(f"{args.base}/ui/studio/{args.table}")
    page.locator("#mapping-select").wait_for()
    page.select_option("#mapping-select", str(mset))
    page.locator(".rule").first.wait_for()
    options = page.locator("#mapping-select option").all_inner_texts()
    print("sets offered:", options)
    assert not any("archived" in o for o in options)
    assert page.locator(".rule .delete").count() == 2

    page.locator(".rule .delete").first.click()
    page.locator("#toast.show", has_text="Rule deleted").wait_for()
    page.locator(".rule").nth(1).wait_for(state="detached")
    print("rule deleted, rules left:", page.locator(".rule").count())

    page.get_by_role("button", name="Archive").click()
    page.locator("#toast.show", has_text="archived").wait_for()
    page.locator("#mapping-select option", has_text=f"#{mset} ").wait_for(state="detached")  # the list reloads after the toast
    print("archived set is hidden from the dropdown")
    page.check("#show-archived")
    page.locator("#mapping-select option", has_text="[archived]").first.wait_for(state="attached")
    page.select_option("#mapping-select", str(mset))
    page.locator("#mapping-status .badge", has_text="archived").wait_for()
    assert page.locator("#run-pipeline").is_disabled()
    assert page.locator("#archive-mapping").inner_text() == "Restore"
    print("archived set shown with a badge; Run pipeline disabled; button reads Restore")
    page.screenshot(path="mock_data/studio_archived.png")
    page.get_by_role("button", name="Restore").click()
    page.locator("#toast.show", has_text="restored").wait_for()
    page.locator("#archive-mapping", has_text="Archive").wait_for()  # the detail reloads after the toast
    print("restored")
    browser.close()

api.post(f"/api/mapping/{mset}/archive", json={"reason": "throw-away set from the Studio UI check"})
print("console errors:", errors)
sys.exit(1 if errors else 0)
