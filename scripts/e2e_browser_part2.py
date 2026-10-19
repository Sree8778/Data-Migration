"""Browser end-to-end, part 2: manual mapping -> approval -> pipeline -> defects -> Excel construction ->
sign-off -> cockpit + SAP sandbox load -> reconciliation certificate. Everything is clicked in the real UI.

    python scripts/e2e_browser_part2.py --table-id 7 [--base http://localhost:8000]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "mock_data" / "screenshots"
OUT = ROOT / "mock_data"
CREATOR, STEWARD = "alice", "data.steward"

COUNTRY_SEED = {"USA": "US", "U.S.A": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "US": "US", "GERMANY": "DE",
                "DEUTSCHLAND": "DE", "DE": "DE", "FRANCE": "FR", "FR": "FR", "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB",
                "GB": "GB", "INDIA": "IN", "IN": "IN", "CANADA": "CA", "CA": "CA"}
PAYMENT_SEED = {"NET30": "NT30", "NET 30": "NT30", "NET60": "NT60", "NET 60": "NT60", "NET90": "NT90", "COD": "CASH", "IMMEDIATE": "CASH"}
RULES = [
    ("BUT000", "BP_EXT", "SQL_EXPRESSION", "CUST_ID", "LPAD(TRIM(CUST_ID), 10, '0')"),
    ("BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME", ""),
    ("ADRC", "STREET", "SQL_EXPRESSION", "ADDR_STREET", "UPPER(TRIM(ADDR_STREET))"),
    ("ADRC", "CITY1", "DIRECT_COPY", "CITY", ""),
    ("ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", ""),
    ("ADRC", "POST_CODE1", "DIRECT_COPY", "POSTAL_CODE", ""),
    ("KNB1", "ZTERM", "VALUE_MAPPING", "PAYMENT_TERMS", ""),
    ("BUT000", "TYPE", "STATIC_VALUE", "", "'2'"),
]
LEGACY_COUNTRY_TO_ISO = {"JAPAN": "JP", "SPAIN": "ES"}
CURRENCY = {"US": "USD", "DE": "EUR", "FR": "EUR", "ES": "EUR", "GB": "GBP", "IN": "INR", "CA": "CAD", "JP": "JPY"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def toast(page, contains: str, timeout: int = 60_000) -> str:
    page.locator("#toast", has_text=contains).wait_for(timeout=timeout)
    return page.inner_text("#toast")


def set_actor(page, name: str) -> None:
    page.fill("#actor", name)
    page.dispatch_event("#actor", "change")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--table-id", type=int, required=True)
    args = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    r: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 950}, accept_downloads=True).new_page()
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.error: {m.text}") if m.type == "error" else None)
        page.on("response", lambda x: problems.append(f"http {x.status}: {x.request.method} {x.url}") if x.status >= 400 else None)

        # ---------------------------------------------------------------- Mapping Studio (manual path)
        page.goto(f"{args.base}/ui/studio/{args.table_id}")
        set_actor(page, CREATOR)
        page.click("#new-set")
        r["new_set_toast"] = toast(page, "Empty draft mapping set")
        mapping_id = int(page.input_value("#mapping-select"))
        r["mapping_set_id"] = mapping_id
        log(f"empty mapping set #{mapping_id} created")

        page.click("#add-rule-toggle")
        for table, column, rtype, source, logic in RULES:
            page.fill("#add-rule-form input[name=target_table]", table)
            page.fill("#add-rule-form input[name=target_column]", column)
            page.select_option("#add-rule-form select[name=rule_type]", rtype)
            page.fill("#add-rule-form input[name=source_column]", source)
            page.fill("#add-rule-form input[name=transformation_logic]", logic)
            page.click("#add-rule-form button[type=submit]")
            toast(page, "Rule added")
            page.locator(".rule").nth(RULES.index((table, column, rtype, source, logic))).wait_for()
        log(f"{len(RULES)} rules added through the form")

        for target, seed in (("ADRC.COUNTRY", COUNTRY_SEED), ("KNB1.ZTERM", PAYMENT_SEED)):
            card = page.locator(".rule", has_text=target)
            card.locator("button.edit").click()
            card.locator("textarea[name=lookups]").fill(json.dumps(seed))
            card.locator("button.save").click()
            toast(page, "Rule updated")
            page.locator(".rule-edit:not(.hidden)").first.wait_for(state="hidden")  # the Studio re-renders after a save
        log("lookups saved through the rule editor")

        page.click("#placeholders")
        r["placeholder_toast"] = toast(page, "placeholder rule")
        page.locator(".rule").nth(10).wait_for()
        page.screenshot(path=str(SHOTS / "05_studio_manual_mapping.png"), full_page=True)
        r["studio_rule_count"] = len(page.query_selector_all(".rule"))
        r["studio_unmapped_mandatory"] = page.inner_text("#unmapped") if page.query_selector("#unmapped h2") else "(none)"

        # ---------------------------------------------------------------- approval (segregation of duties)
        page.click("#submit-mapping")
        toast(page, "Submitted")
        page.click("#approve-mapping")  # still alice = creator: must be refused
        r["self_approval_toast"] = toast(page, "SEGREGATION")
        set_actor(page, STEWARD)
        page.click("#approve-mapping")
        toast(page, "approved")
        r["mapping_status"] = page.inner_text("#mapping-status")
        page.screenshot(path=str(SHOTS / "06_studio_approved.png"), full_page=True)
        log(f"approval: self-approval refused, steward approved -> {r['mapping_status']!r}")

        # ---------------------------------------------------------------- pipeline
        started = time.time()
        page.click("#run-pipeline")
        page.wait_for_url(re.compile(r"/ui/governance/\d+"), timeout=300_000)
        run_id = int(page.url.rsplit("/", 1)[1])
        r["run_id"], r["pipeline_seconds"] = run_id, round(time.time() - started, 1)
        page.wait_for_selector("#defects table tbody tr")
        r["gate_blocked"] = page.inner_text("#gate").replace("\n", " ")
        r["defects_before"] = {row.query_selector_all("td")[1].inner_text(): int(row.query_selector_all("td")[3].inner_text().replace(",", "") or 0)
                               for row in page.query_selector_all("#defects table tbody tr")}
        r["quality_before"] = page.inner_text("#defects p")
        page.screenshot(path=str(SHOTS / "07_governance_blocked.png"), full_page=True)
        log(f"run #{run_id} done in {r['pipeline_seconds']}s; gate: {r['gate_blocked'][:60]}...")

        # ---------------------------------------------------------------- Data Construction
        page.click("#tab-construction")
        page.wait_for_selector("#construction-cards .stat")
        r["construction_cards_before"] = [s.inner_text().replace("\n", " ") for s in page.query_selector_all("#construction-cards .stat")]
        r["construction_fields_before"] = [row.inner_text().replace("\t", " | ").replace("\n", " ") for row in page.query_selector_all("#construction-fields table tbody tr")]
        page.screenshot(path=str(SHOTS / "08_construction_before.png"), full_page=True)

        with page.expect_download() as dl:
            page.click("#download-template")
        template = OUT / "construction_template_downloaded.xlsx"
        dl.value.save_as(template)
        wb = load_workbook(template)
        ws = wb["Construction"]
        headers = [c.value for c in ws[1]]
        r["template_headers"] = headers
        r["template_rows"] = ws.max_row - 1
        col = {h: i + 1 for i, h in enumerate(headers)}
        filled = {"cells": 0, "left_blank": 0}
        for row_no in range(2, ws.max_row + 1):
            legacy = (ws.cell(row=row_no, column=col["LEGACY_COUNTRY"]).value or "").strip() if "LEGACY_COUNTRY" in col else ""
            iso = COUNTRY_SEED.get(legacy.upper()) or LEGACY_COUNTRY_TO_ISO.get(legacy.upper())
            for h in headers:
                cell = ws.cell(row=row_no, column=col[h])
                if cell.protection.locked is not False:
                    continue
                if h == "BU_GROUP":
                    value = "BP01"
                elif h == "AKONT":
                    value = "113100"
                elif h in ("BUKRS", "VKORG", "VTWEG", "SPART"):
                    value = {"BUKRS": "1000", "VKORG": "1010", "VTWEG": "10", "SPART": "00"}[h]
                elif h == "WAERS":
                    value = CURRENCY.get(iso or "US", "USD")
                elif h == "COUNTRY" or h.endswith(".COUNTRY"):
                    value = iso or "US"          # steward decision for blank / unknown legacy countries
                elif h == "CITY1" or h.endswith(".CITY1"):
                    value = "UNKNOWN"
                elif h == "ZTERM" or h.endswith(".ZTERM"):
                    value = "NT30"               # 'WHEN-RECEIVED' is not a valid term: steward picks net 30
                else:
                    value = None
                if value is None:
                    filled["left_blank"] += 1
                else:
                    cell.value, filled["cells"] = value, filled["cells"] + 1
        r["steward_filled"] = filled
        completed = OUT / "construction_template_completed.xlsx"
        wb.save(completed)
        log(f"template: {r['template_rows']} records, steward filled {filled['cells']} cells")

        page.set_input_files("#construction-file", str(completed))
        r["upload_toast"] = toast(page, "cells merged", timeout=120_000)
        r["revalidate_toast"] = toast(page, "Re-validated", timeout=180_000)
        page.wait_for_selector("#upload-result .banner")
        r["upload_result"] = page.inner_text("#upload-result").replace("\n", " ")[:600]
        page.wait_for_timeout(800)
        r["gate_after"] = page.inner_text("#gate").replace("\n", " ")
        r["construction_log"] = [row.inner_text().replace("\t", " | ").replace("\n", " ") for row in page.query_selector_all("#construction-log table tbody tr")]
        page.screenshot(path=str(SHOTS / "09_construction_after.png"), full_page=True)
        page.click("#tab-defects")
        page.wait_for_selector("#defects table tbody tr")
        r["defects_after"] = {row.query_selector_all("td")[1].inner_text(): row.query_selector_all("td")[3].inner_text()
                              for row in page.query_selector_all("#defects table tbody tr")}
        page.screenshot(path=str(SHOTS / "10_governance_after.png"), full_page=True)
        log(f"after construction: {r['gate_after'][:70]}")

        # ---------------------------------------------------------------- sign-off + load
        if "READY FOR LOAD" in r["gate_after"]:
            set_actor(page, STEWARD)
            page.click("#sign-off")
            r["signoff_toast"] = toast(page, "signed off")
            page.select_option("#load-mode", "cockpit")
            with page.expect_download() as dl2:
                page.click("#load")
            cockpit = OUT / "migration_cockpit_downloaded.xlsx"
            dl2.value.save_as(cockpit)
            wb2 = load_workbook(cockpit, read_only=True)
            r["cockpit_sheets"] = {name: sum(1 for _ in wb2[name].iter_rows(values_only=True)) - 1 for name in wb2.sheetnames}
            page.select_option("#load-mode", "sap:sandbox")
            page.click("#load")
            r["sap_toast"] = toast(page, "Simulated SAP load", timeout=300_000)
            log(f"load: {r['sap_toast']}")

            page.goto(f"{args.base}/ui/certificate/{run_id}")
            page.wait_for_selector("#checks table tbody tr", timeout=60_000)
            r["certificate_verdict"] = page.inner_text("#verdict").replace("\n", " ")
            r["certificate_counts"] = [s.inner_text().replace("\n", " ") for s in page.query_selector_all("#counts .stat")]
            r["certificate_dropoff"] = [row.inner_text().replace("\t", " | ").replace("\n", " ")[:160] for row in page.query_selector_all("#dropoff table tbody tr")]
            r["certificate_failed_checks"] = [row.inner_text().replace("\t", " | ") for row in page.query_selector_all("#checks table tbody tr") if "FAIL" in row.inner_text()]
            r["certificate_buttons_enabled"] = {b: page.is_enabled(b) for b in ("#dl-cockpit", "#dl-html", "#dl-md")}
            page.screenshot(path=str(SHOTS / "11_certificate.png"), full_page=True)
        browser.close()

    r["browser_problems"] = problems
    print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
