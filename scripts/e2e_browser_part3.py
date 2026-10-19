"""Browser end-to-end, part 3: SAP sandbox load from the Governance Hub, then the reconciliation certificate.

    python scripts/e2e_browser_part3.py --run-id 4
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
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--user", default="data.steward")
    args = ap.parse_args()
    problems: list[str] = []
    r: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 950}, accept_downloads=True).new_page()
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.error: {m.text}") if m.type == "error" else None)
        page.on("response", lambda x: problems.append(f"http {x.status}: {x.request.method} {x.url}") if x.status >= 400 else None)

        page.goto(f"{args.base}/ui/governance/{args.run_id}")
        page.fill("#actor", args.user)
        page.dispatch_event("#actor", "change")
        page.locator("#defects table").wait_for()
        r["gate"] = page.inner_text("#gate").replace("\n", " ")
        page.select_option("#load-mode", "sap:sandbox")
        page.click("#load")
        page.locator("#toast", has_text="Simulated SAP load").wait_for(timeout=300_000)
        r["sap_toast"] = page.inner_text("#toast")
        page.screenshot(path=str(SHOTS / "12_governance_loaded.png"), full_page=True)

        page.goto(f"{args.base}/ui/certificate/{args.run_id}")
        page.locator("#checks table tbody tr").first.wait_for(timeout=60_000)
        r["verdict"] = page.inner_text("#verdict").replace("\n", " ")
        r["counts"] = [s.inner_text().replace("\n", " ") for s in page.query_selector_all("#counts .stat")]
        r["dropoff"] = [row.inner_text().replace("\t", " | ").replace("\n", " ")[:230] for row in page.query_selector_all("#dropoff table tbody tr")]
        r["failed_checks"] = [row.inner_text().replace("\t", " | ") for row in page.query_selector_all("#checks table tbody tr") if row.inner_text().startswith("FAIL")]
        r["checks_total"] = len(page.query_selector_all("#checks table tbody tr"))
        r["lineage"] = page.inner_text("#lineage").replace("\n", " ")[:400]
        r["buttons_enabled"] = {b: page.is_enabled(b) for b in ("#dl-cockpit", "#dl-html", "#dl-md")}
        page.screenshot(path=str(SHOTS / "13_certificate.png"), full_page=True)
        browser.close()
    r["browser_problems"] = problems
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
