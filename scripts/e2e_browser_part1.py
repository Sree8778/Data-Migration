"""Browser end-to-end, part 1: register + upload the mock extract, profile it, look at the key dialog.

    python scripts/e2e_browser_part1.py [--base http://localhost:8000]

Drives the real UI in the installed Edge (Playwright, headless). Screenshots go to mock_data/screenshots.
Reports console errors / failed requests, which is the point: the UI scripts had never run in a browser.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "mock_data" / "screenshots"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--user", default="alice")
    ap.add_argument("--system", default="MOCK_LEGACY_ORACLE", help="registering an existing system+table REPLACES its source file")
    ap.add_argument("--table", default="MOCK_AR_CUSTOMERS")
    args = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    result: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.method} {r.url} {r.failure}"))
        page.on("response", lambda r: problems.append(f"http {r.status}: {r.request.method} {r.url}") if r.status >= 400 else None)

        page.goto(args.base + "/ui/sources")
        page.fill("#actor", args.user)
        page.dispatch_event("#actor", "change")
        page.wait_for_selector("#status-banner")
        result["banner"] = page.inner_text("#status-banner")
        page.screenshot(path=str(SHOTS / "01_workspace.png"), full_page=True)

        # ---- register + upload
        page.fill("input[name=system_name]", args.system)
        page.select_option("select[name=system_type]", "ORACLE")
        page.fill("input[name=table_name]", args.table)
        page.fill("input[name=primary_key_columns]", "CUST_ID")
        page.set_input_files("input[name=file]", str(ROOT / "mock_data" / "legacy_ar_customers.csv"))
        page.click("#register-form button[type=submit]")
        page.wait_for_selector("#toast.show")
        result["register_toast"] = page.inner_text("#toast")
        row = page.locator("#tables table tbody tr", has_text=args.table).first
        row.wait_for()
        link = row.locator("a[href^='/ui/profiler/']").get_attribute("href")
        result["table_id"] = int(link.rsplit("/", 1)[1])
        page.screenshot(path=str(SHOTS / "02_registered.png"), full_page=True)

        # ---- profile
        page.goto(args.base + link)
        page.fill("#actor", args.user)
        page.dispatch_event("#actor", "change")
        page.click("#run-profile")
        page.wait_for_selector("#summary .stat", timeout=120_000)
        result["profile_cards"] = [s.inner_text().replace("\n", " ") for s in page.query_selector_all("#summary .stat")]
        rows = page.query_selector_all("#columns table tbody tr")
        result["profile_columns"] = len(rows)
        result["profile_health"] = {r.query_selector("td").inner_text(): r.query_selector_all("td")[7].inner_text() for r in rows}
        result["profile_flags"] = {r.query_selector("td").inner_text(): r.query_selector_all("td")[8].inner_text() for r in rows
                                   if r.query_selector_all("td")[8].inner_text().strip()}
        page.screenshot(path=str(SHOTS / "03_profiler.png"), full_page=True)

        # ---- API key stack dialog
        page.click("#key-indicator")
        page.wait_for_selector("#key-modal[open]")
        result["key_indicator"] = page.inner_text("#key-indicator")
        result["key_dialog_rows"] = [r.inner_text().replace("\t", " | ").replace("\n", " ") for r in page.query_selector_all("#key-list table tbody tr")]
        page.screenshot(path=str(SHOTS / "04_key_stack.png"))
        page.keyboard.press("Escape")
        browser.close()

    result["browser_problems"] = problems
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
