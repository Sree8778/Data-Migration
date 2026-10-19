"""Edge/Playwright check of the multi-format source registration UI (preview, read options, control total, errors).

    python scripts/e2e_browser_sources.py [--base http://localhost:8000]

Registers tables UI_SEMI / UI_XLSX under system UI_CHECK_SYS (upserted, so re-running is safe). Exit 1 on any
failed assertion or unexpected browser console error (the one 422 from the deliberately invalid .xlsx is expected).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
BASE = ap.parse_args().base
WORK = Path(tempfile.mkdtemp(prefix="ui_sources_"))

rows = [["CUST_ID", "CUST_NAME", "ZIP", "AMOUNT"], ["1001", "Müller GmbH", "02134", "10.50"], ["1002", "Café Ñandú", "10001", "7"]]
(WORK / "semicolon.txt").write_bytes(("\r\n".join(";".join(r) for r in rows) + "\r\n").encode("cp1252"))
wb = Workbook()
wb.active.title = "Info"
wb.active.append(["Export notes"])
data = wb.create_sheet("Customers")
for r in rows:
    data.append(r)
wb.save(WORK / "customers.xlsx")
(WORK / "bad.xlsx").write_text("not a workbook")

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.add_init_script("localStorage.setItem('migration.user', 'ui.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/ui/sources")
    page.locator("#status-banner").wait_for()

    # 1. semicolon / cp1252 text file: format, encoding and delimiter are detected
    page.fill("input[name=system_name]", "UI_CHECK_SYS")
    page.fill("input[name=table_name]", "UI_SEMI")
    page.set_input_files("#reg-file", str(WORK / "semicolon.txt"))
    page.locator("#preview-table table").wait_for()
    detected = page.locator("#detected").inner_text()
    print("detected:", detected.replace("\n", " | "))
    assert "delimited" in detected and "cp1252" in detected and "delimiter: ;" in detected, detected
    assert page.locator("#opt-delimiter").input_value() == ";"
    preview = page.locator("#preview-table").inner_text()
    assert "Müller GmbH" in preview and "02134" in preview, preview  # accents intact, leading zero kept
    page.fill("#opt-expected", "2")
    page.get_by_role("button", name="Register & upload").click()
    page.locator("#toast.show").wait_for()
    toast = page.locator("#toast").inner_text()
    print("toast:", toast)
    assert "2 rows" in toast and "delimited" in toast, toast
    assert page.locator("#read-options.hidden").count() == 1

    # 2. Excel workbook with a sheet picker
    page.fill("input[name=system_name]", "UI_CHECK_SYS")  # form.reset() cleared it
    page.fill("input[name=table_name]", "UI_XLSX")
    page.set_input_files("#reg-file", str(WORK / "customers.xlsx"))
    page.locator("#preview-table table").wait_for()
    sheets = page.locator("#opt-sheet option").all_inner_texts()
    print("sheets:", sheets)
    assert "Info" in sheets and "Customers" in sheets
    page.select_option("#opt-sheet", "Customers")
    page.click("#refresh-preview")
    page.locator("#preview-table th", has_text="CUST_NAME").wait_for()
    page.get_by_role("button", name="Register & upload").click()
    page.locator("#toast.show", has_text="excel").wait_for()
    print("toast:", page.locator("#toast").inner_text())

    # 3. a bad file shows a clear error in the preview, not a crash
    page.set_input_files("#reg-file", str(WORK / "bad.xlsx"))
    page.locator("#preview-table .banner.red").wait_for()
    print("bad file:", page.locator("#preview-table .banner.red").inner_text())

    page.reload()
    page.locator("#tables table").wait_for()
    listing = page.locator("#tables").inner_text()
    assert "UI_SEMI" in listing and "UI_XLSX" in listing and "excel" in listing
    browser.close()

unexpected = [e for e in errors if "422" not in e]
print("console errors (excluding the expected 422 for bad.xlsx):", unexpected)
sys.exit(1 if unexpected else 0)
