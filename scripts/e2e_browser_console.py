"""Edge/Playwright check of the Management Center console: explorer, tabs, menus, toolbar, restore, key dialog.

    python scripts/e2e_browser_console.py [--base http://localhost:8000] [--table AR_CUSTOMERS]
"""
from playwright.sync_api import sync_playwright
import csv, io
from pathlib import Path

import httpx

_api = httpx.Client(base_url="http://localhost:8000", headers={"X-User": "console.tester"}, timeout=120)
_tables = {t["table_name"]: t["table_id"] for t in _api.get("/api/tables").json() if t["system_name"] == "LEGACY_ORACLE_AR"}
if "AR_CUSTOMERS" not in _tables:                       # the check needs one table with a mapping set; create it if the workspace is empty
    _rows = list(csv.DictReader(open(Path(__file__).resolve().parent.parent / "mock_data" / "legacy_ar_customers.csv", newline="", encoding="utf-8")))[:200]
    _buf = io.StringIO(); _w = csv.DictWriter(_buf, fieldnames=list(_rows[0]), lineterminator=chr(10)); _w.writeheader(); _w.writerows(_rows)
    _r = _api.post("/api/sources/register", data={"system_name": "LEGACY_ORACLE_AR", "table_name": "AR_CUSTOMERS", "primary_key_columns": "CUST_ID"},
                   files={"file": ("ar.csv", _buf.getvalue().encode())})
    assert _r.status_code == 201, _r.text
    _tables["AR_CUSTOMERS"] = _r.json()["table_id"]
    _api.post(f"/api/profile/{_tables['AR_CUSTOMERS']}").raise_for_status()
_api.post("/api/mapping-sets", json={"source_table_id": _tables["AR_CUSTOMERS"]}).raise_for_status()
TID = _tables["AR_CUSTOMERS"]

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1400, "height": 860})
    errs=[]
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.add_init_script("if(!localStorage.getItem('migration.user')) localStorage.setItem('migration.user','alice')")
    pg.goto("http://localhost:8000/")
    pg.evaluate("localStorage.removeItem('migration.console.tabs'); localStorage.removeItem('migration.console.open')")
    pg.reload()
    pg.locator("#tabstrip .tab").first.wait_for()
    # expand Sources > LEGACY_ORACLE_AR > AR_CUSTOMERS
    pg.locator('li[data-id="sys:LEGACY_ORACLE_AR"] > .node > .twist').click()
    pg.locator(f'li[data-id="t{TID}"] > .node > .twist').click()
    pg.locator(f'li[data-id="t{TID}s"] > .node > .twist').click()
    pg.locator(f'li[data-id="t{TID}"] > .node').click()
    print("toolbar enabled after selecting table:", [b.inner_text() for b in pg.locator(".ib:not([disabled])").all()])
    pg.locator(f'li[data-id="t{TID}m"] > .node').dblclick()
    pg.locator("#tabstrip .tab", has_text="Mapping - AR_CUSTOMERS").wait_for()
    fr = pg.frame_locator("iframe.active")
    fr.locator("#mapping-select").wait_for()
    fr.locator("#mapping-status .badge").wait_for()               # the Studio has loaded a mapping set (a fresh one may have no rules yet)
    print("studio tab: topbar present?", fr.locator(".topbar").count(), "| sets:", fr.locator("#mapping-select option").all_inner_texts()[:5])
    pg.screenshot(path="mock_data/console_studio.png")
    # link inside a page opens a NEW tab in the console
    pg.locator("#tabstrip .tab", has_text="Start").click()
    pg.frame_locator("iframe.active").get_by_role("link", name="Register a legacy source").click()
    pg.locator("#tabstrip .tab", has_text="Sources & Runs").wait_for()
    print("tabs:", pg.locator("#tabstrip .tab .title").all_inner_texts())
    # menus
    pg.get_by_role("menuitem", name="Window").click()
    print("window menu:", pg.locator("#window-menu button").all_inner_texts())
    pg.keyboard.press("Escape")
    # close tab with middle click + alt+w
    pg.keyboard.press("Alt+w")
    print("after Alt+W:", pg.locator("#tabstrip .tab .title").all_inner_texts())
    # reload restores tabs
    pg.reload(); pg.locator("#tabstrip .tab").first.wait_for()
    print("restored after reload:", pg.locator("#tabstrip .tab .title").all_inner_texts())
    # keys dialog
    pg.get_by_role("button", name="Keys", exact=True).click()
    pg.locator("#key-modal[open]").wait_for(); print("key dialog opens")
    pg.keyboard.press("Escape")
    print("errors:", errs)
    b.close()
