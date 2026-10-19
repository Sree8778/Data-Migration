"""Edge/Playwright check of the CLOUD FILE STORAGE path of the Add Source wizard: Azure Blob (Azurite), S3 API (CloudServer) and SFTP.

    docker compose -f docker-compose.servers.yml --profile storage up -d
    python scripts/e2e_browser_storage.py [--base http://localhost:8000]

For each server: uploads the fixture files with the vendor SDK, then in the browser adds a source through the wizard (provider, credentials,
Test, browse folders, tick files, Finish), and verifies through the API that the tables landed with the right rows and provenance. For SFTP
it also walks the host-key confirmation: the first Test refuses to log in and shows the fingerprint, 'Trust this key' pins it. Servers that
are not running are skipped. Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_storage import STORES, port_open  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
BASE = ap.parse_args().base
api = httpx.Client(base_url=BASE, headers={"X-User": "storage.tester"}, timeout=120)
NAMES = {"azure_blob": "E2E_AZURE", "s3": "E2E_S3", "sftp": "E2E_SFTP"}
errors: list[str] = []
ran = 0

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 940})
    page.add_init_script("localStorage.setItem('migration.user', 'storage.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    nxt = page.locator("#wz-next")

    for kind, store in STORES.items():
        if not port_open(store.host, store.port):
            print(f"skipping {kind}: server not running")
            continue
        ran += 1
        store.setup(store)
        name = NAMES[kind]
        api.delete(f"/api/connections/{name}")
        page.get_by_role("button", name="New Source").click()
        page.locator("#s-name").fill(name)
        kinds = page.locator("#s-kind optgroup[label^='Cloud file storage'] option").all_inner_texts()
        assert any("Azure" in k for k in kinds) and any("S3" in k for k in kinds) and any("SFTP" in k for k in kinds), kinds
        page.select_option("#s-kind", kind)
        assert page.locator("#wz-body input[readonly]").input_value()
        nxt.click()
        assert nxt.is_disabled()
        page.fill("#c-host", store.host); page.fill("#c-port", str(store.port)); page.fill("#c-user", store.user); page.fill("#c-password", store.password)
        if store.database:
            page.fill("#c-database", store.database)
        if kind in ("azure_blob", "s3"):
            page.locator('[data-opt="https"]').uncheck()
            page.locator('[data-opt="path_style"]').check()
        if kind == "s3":
            page.locator('[data-opt="region"]').fill("us-east-1")
        page.click("#c-test")
        if kind == "sftp":
            page.locator("#c-trust").wait_for()                     # the server identity is shown and must be confirmed
            assert "SHA256:" in page.locator(".banner.amber").inner_text() and nxt.is_disabled()
            print("sftp asks to trust:", page.locator(".banner.amber code").inner_text())
            page.click("#c-trust")                                  # pins the key and tests again
        page.locator("#c-status.ok").wait_for()
        print(kind, "test:", page.locator("#c-status").inner_text())
        nxt.click()

        # ---- browse: container, folder, files
        page.locator("#f-list .wz-tbl").first.wait_for()
        assert nxt.is_disabled()
        rows = page.locator("#f-list .wz-tbl").all_inner_texts()
        print(kind, "root:", [r.strip().split("\n")[0] for r in rows][:8])
        assert any("sub" in r for r in rows) and any("customers.csv" in r for r in rows)
        page.locator('#f-list input[data-key="customers.csv"]').check()
        page.locator('#f-list input[data-key="semi_cp1252.txt"]').check()
        page.locator('#f-list a[data-go="sub/"]').click()
        page.locator('#f-list input[data-key="sub/ar_customers.csv.gz"]').wait_for()
        page.locator('#f-list input[data-key="sub/ar_customers.csv.gz"]').check()
        assert page.locator("#f-count").inner_text() == "3 selected"
        page.locator('#f-crumbs a[data-go=""]').click()               # back to the top; selections are kept
        page.locator('#f-list input[data-key="customers.csv"]').wait_for()
        assert page.locator('#f-list input[data-key="customers.csv"]').is_checked()
        nxt.click()
        page.uncheck("#a-studio")
        nxt.click()
        summary = page.locator("#wz-body").inner_text()
        assert "customers.csv" in summary and "ar_customers.csv.gz" in summary and store.password not in summary
        page.screenshot(path=f"mock_data/wizard_storage_{kind}.png")
        nxt.click()
        page.locator("#wizard[open]").wait_for(state="detached", timeout=120000)
        page.locator("#toast.show", has_text="Imported 3 file(s)").wait_for()

        tables = {t["table_name"]: t for t in api.get("/api/tables").json() if t["system_name"] == name}
        print(kind, "registered:", {k: v["rows_landed"] for k, v in tables.items()})
        assert set(tables) == {"CUSTOMERS", "SEMI_CP1252", "AR_CUSTOMERS"}
        assert tables["CUSTOMERS"]["rows_landed"] == 3 and tables["SEMI_CP1252"]["rows_landed"] == 2 and tables["AR_CUSTOMERS"]["rows_landed"] == 3
        snap = api.get(f"/api/tables/{tables['SEMI_CP1252']['table_id']}/snapshot").json()
        assert snap["detection"]["encoding"] == "cp1252" and snap["origin"]["kind"] == "storage" and snap["origin"]["provider"] == kind
        assert store.password not in api.get("/api/connections").text
    browser.close()

assert ran, "no storage server is running: docker compose -f docker-compose.servers.yml --profile storage up -d"
unexpected = [e for e in errors if "422" not in e and "502" not in e and "401" not in e]
print("console errors:", unexpected)
sys.exit(1 if unexpected else 0)
