"""Edge/Playwright check of the Add Source / Add Target wizards in the Management Center.

    python scripts/e2e_browser_wizard.py [--base http://localhost:8000]

Adds source WIZ_SYS / WIZ_SEMI (cp1252, semicolon) and WIZ_SYS / WIZ_XLSX (Excel, sheet picker) through the wizard, checks the
explorer, the tabs, the profile and the validation messages, then walks the target wizard for the sandbox. Re-running is safe
(registering the same table name replaces its snapshot). Exit 1 on any failed assertion or unexpected console error.
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
WORK = Path(tempfile.mkdtemp(prefix="wizard_"))

rows = [["CUST_ID", "CUST_NAME", "ZIP", "AMOUNT"], ["1001", "Müller GmbH", "02134", "10.50"], ["1002", "Café Ñandú", "10001", "7"], ["1003", "Zoë AG", "80331", "3"]]
(WORK / "customers_semicolon.txt").write_bytes(("\r\n".join(";".join(r) for r in rows) + "\r\n").encode("cp1252"))
(WORK / "repeat_key.csv").write_text("ID,NAME\n1,A\n1,B\n", encoding="utf-8")
wb = Workbook()
wb.active.title = "Info"
wb.active.append(["Export notes"])
data = wb.create_sheet("Customers")
for r in rows:
    data.append(r)
wb.save(WORK / "customers.xlsx")

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.add_init_script("localStorage.setItem('migration.user', 'wizard.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    next_btn = page.locator("#wz-next")

    # ------------------------------------------------------------ 1. semicolon / cp1252 text file
    page.get_by_role("button", name="New Source").click()
    page.locator("#wizard[open]").wait_for()
    assert "Add Source Connection Wizard" in page.locator("#wz-title").inner_text()
    steps = page.locator("#wz-steps li").all_inner_texts()
    assert steps == ["Select provider", "Set connection", "Select table(s)", "Actions", "Summary"], steps
    assert next_btn.is_disabled()                                   # a name is required
    db_options = page.locator("#s-kind option").all_inner_texts()
    assert any("Oracle" in o for o in db_options) and any("PostgreSQL" in o for o in db_options)   # databases are offered (their own check is e2e_browser_wizard_db.py)
    page.fill("#s-name", "WIZ_SYS")
    next_btn.click()
    assert next_btn.is_disabled()                                   # a tested file is required
    page.set_input_files("#p-file", str(WORK / "customers_semicolon.txt"))
    page.locator("#p-status.ok").wait_for()
    status = page.locator("#p-status").inner_text()
    print("test:", status)
    assert "delimited" in status and "cp1252" in status and "delimiter ;" in status, status
    assert page.locator("#p-delimiter").input_value() == ";"
    assert page.locator("#p-sheet").count() == 0 and page.locator("#p-layout").count() == 0   # only the options that apply to this format
    page.locator('tr[data-key="encoding"] input').focus()
    assert "Text encoding" in page.locator("#pg-help").inner_text()
    next_btn.click()
    assert page.locator("#t-name").input_value() == "CUSTOMERS_SEMICOLON"
    page.fill("#t-name", "WIZ_SEMI")
    page.locator('.t-key[data-col="CUST_ID"]').check()
    page.fill("#t-expected", "3")
    next_btn.click()                                                 # actions
    page.uncheck("#a-studio")
    next_btn.click()                                                 # summary
    summary = page.locator("#wz-body").inner_text()
    assert "WIZ_SYS" in summary and "WIZ_SEMI" in summary and "cp1252" in summary and "CUST_ID" in summary and "3 rows" in summary, summary
    page.screenshot(path="mock_data/wizard_summary.png")
    assert next_btn.inner_text() == "Finish"
    next_btn.click()
    page.locator("#wizard[open]").wait_for(state="detached")
    page.locator("#tabstrip .tab", has_text="Profiler - WIZ_SEMI").wait_for()
    assert page.locator('#tree li[data-id^="t"] .label', has_text="WIZ_SEMI").count() >= 1
    frame = page.frame_locator("iframe.active")
    frame.locator("#summary .stat").first.wait_for()
    print("profile tab opened; explorer shows the new table")

    # ------------------------------------------------------------ 2. Excel with a sheet picker
    page.get_by_role("button", name="New Source").click()
    page.fill("#s-name", "WIZ_SYS")
    next_btn.click()
    page.set_input_files("#p-file", str(WORK / "customers.xlsx"))
    page.locator("#p-status.ok").wait_for()
    sheets = page.locator("#p-sheet option").all_inner_texts()
    print("sheets:", sheets)
    assert "Info" in sheets and "Customers" in sheets
    assert page.locator("#p-delimiter").count() == 0                 # not offered for Excel
    page.select_option("#p-sheet", "Customers")
    page.click("#p-test")
    page.locator("#p-status.ok").wait_for()
    assert "4 columns" in page.locator("#p-status").inner_text()
    next_btn.click()
    page.fill("#t-name", "WIZ_XLSX")
    next_btn.click(); page.uncheck("#a-profile"); page.uncheck("#a-studio"); next_btn.click(); next_btn.click()
    page.locator("#wizard[open]").wait_for(state="detached")
    page.locator('#tree li[data-id^="t"] .label', has_text="WIZ_XLSX").first.wait_for()
    print("excel table registered")

    # ------------------------------------------------------------ 3. a repeated key is rejected at Finish with a clear message
    page.get_by_role("button", name="New Source").click()
    page.fill("#s-name", "WIZ_SYS"); next_btn.click()
    page.set_input_files("#p-file", str(WORK / "repeat_key.csv")); page.locator("#p-status.ok").wait_for(); next_btn.click()
    page.fill("#t-name", "WIZ_REPEAT"); page.locator('.t-key[data-col="ID"]').check()
    next_btn.click(); page.uncheck("#a-profile"); page.uncheck("#a-studio"); next_btn.click(); next_btn.click()
    page.locator("#wz-error:not(.hidden)").wait_for()
    msg = page.locator("#wz-error").inner_text()
    print("rejected:", msg)
    assert "does not identify rows uniquely" in msg and page.locator("#wizard[open]").count() == 1
    page.click("#wz-cancel")

    # ------------------------------------------------------------ 4. target wizard (sandbox)
    page.get_by_role("button", name="New Target").click()
    page.locator("#wizard[open]").wait_for()                       # the wizard opens after it has loaded the available database types
    assert "Add Target Connection Wizard" in page.locator("#wz-title").inner_text()
    kinds = page.locator("#g-kind option").all_inner_texts()
    print("targets:", kinds)
    assert len(kinds) == 3
    next_btn.click()
    assert next_btn.is_disabled()                                    # the connection must be tested first
    page.click("#g-test")
    page.locator("#g-status.ok").wait_for()
    print("target test:", page.locator("#g-status").inner_text())
    next_btn.click()                                                 # select object (catalog is seeded in this workspace)
    assert "BUSINESS_PARTNER" in page.locator("#wz-body").inner_text()
    next_btn.click(); next_btn.click()
    assert "tested OK" in page.locator("#wz-body").inner_text()
    next_btn.click()
    page.locator("#wizard[open]").wait_for(state="detached")
    page.locator("#toast.show", has_text="checked").wait_for()
    # live target is not configured here: the wizard says so and blocks Next
    page.get_by_role("button", name="New Target").click()
    page.select_option("#g-kind", "live")
    next_btn.click()
    detail = page.locator(".pg").inner_text()
    print("live:", " | ".join(detail.split()).encode("ascii", "replace").decode())
    assert "not configured on the server" in detail and next_btn.is_disabled()   # honest about it, and Next stays blocked
    assert page.locator("#g-test").is_disabled()                     # nothing to test until the server has SAP credentials
    page.click("#wz-cancel")
    browser.close()

unexpected = [e for e in errors if "422" not in e]
print("console errors (excluding expected 422s):", unexpected)
sys.exit(1 if unexpected else 0)
