"""Edge/Playwright check of the Combine tables page (no AI, no key needed):

    python scripts/e2e_browser_combine.py [--base http://localhost:8000]

Lands the mock legacy customers, a payment-terms table and a contacts table (several per customer), then in the browser:
  1  join customers to payment terms (left join): the preview counts the customers that find no partner
  2  join customers to contacts: the preview is BLOCKED because customers would repeat, and Create is not offered
  3  allow it with a filter, preview again, create the table, open it in the profiler
Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
args = ap.parse_args()
BASE = args.base
api = httpx.Client(base_url=BASE, headers={"X-User": "g3.ui"}, timeout=300)
stamp = time.strftime("%H%M%S")


def csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header); w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def land(system, name, header, rows, key=None):
    r = api.post("/api/sources/register", data={"system_name": system, "table_name": name, **({"primary_key_columns": key} if key else {})},
                 files={"file": (f"{name}.csv", csv_bytes(header, rows))})
    assert r.status_code == 201, r.text
    return r.json()["table_id"]


customers = list(csv.DictReader(open(ROOT / "mock_data" / "legacy_ar_customers.csv", newline="", encoding="utf-8")))[:500]
sys_name = f"G3_UI_{stamp}"
c_id = land(sys_name, f"CUST_{stamp}", list(customers[0]), [list(r.values()) for r in customers], "CUST_ID")
codes = {"NET30": "Net 30 days", "NET60": "Net 60 days", "COD": "Cash on delivery"}                   # NET45 etc. deliberately missing
t_id = land(sys_name, f"TERMS_{stamp}", ["CODE", "TEXT"], [[k, v] for k, v in codes.items()], "CODE")
no_partner = sum(1 for r in customers if r["PAYMENT_TERMS"].strip().upper() not in codes)
contacts = [[f"K{i}_{n}", r["CUST_ID"], f"Contact {i}.{n}"] for i, r in enumerate(customers[:50]) for n in range(1 + (i % 2))]       # every second customer has two contacts
k_id = land(sys_name, f"CONTACTS_{stamp}", ["CONTACT_ID", "CUST_ID", "CONTACT"], contacts, "CONTACT_ID")
print(f"customers {len(customers)}, of which {no_partner} have payment terms not in the terms table; contacts {len(contacts)}")

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 1200})
    page.add_init_script("localStorage.setItem('migration.user', 'g3.ui')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{BASE}/ui/combine")
    page.locator("#base-table option").first.wait_for(state="attached")
    page.select_option("#base-table", str(c_id))

    # ---- 1. customers + payment terms
    page.click("#add-join")
    card = page.locator("[data-join='0']")
    card.locator('select[data-f="table"]').select_option(str(t_id))
    card.locator('select[data-f="left"]').select_option("a.PAYMENT_TERMS")
    card.locator('select[data-f="right"]').select_option("b.CODE")
    page.click("#preview")
    page.locator("#result .cards").wait_for()
    text = page.locator("#result").inner_text()
    print("preview 1:", " ".join(text.split())[:240])
    assert f"{len(customers)}" in text and "find no partner" in text.lower()
    assert str(no_partner) in page.locator("#result table").first.inner_text()
    assert page.locator("#create-card").is_visible()
    page.screenshot(path="mock_data/combine_preview.png")

    # ---- 2. customers + contacts: blocked
    page.click('[data-join="0"] [data-act="del-join"]')
    page.click("#add-join")
    card = page.locator("[data-join='0']")
    card.locator('select[data-f="table"]').select_option(str(k_id))
    card.locator('select[data-f="left"]').select_option("a.CUST_ID")
    card.locator('select[data-f="right"]').select_option("b.CUST_ID")
    page.click("#preview")
    page.locator("#result .banner.red").wait_for()
    blocked = page.locator("#result .banner.red").inner_text()
    print("blocked:", blocked[:200])
    assert "Cannot be created" in blocked and "repeated" in blocked and not page.locator("#create-card").is_visible()

    # ---- 3. allow it, filter to Germany-style countries, create
    page.check("#allow-fanout")
    page.click("#add-filter")
    page.locator('[data-filter="0"] select[data-f="column"]').select_option("a.COUNTRY")
    page.locator('[data-filter="0"] select[data-f="op"]').select_option("is_blank")
    page.click("#preview")
    page.locator("#result .cards").wait_for()
    page.locator("#create-card").wait_for(state="visible")
    result = page.locator("#result").inner_text()
    print("preview 3:", " ".join(result.split())[:200])
    assert "allowed, so records repeat" in result
    page.fill("#c-table", f"CUST_CONTACTS_{stamp}")
    page.fill("#c-system", sys_name + "_COMBINED")
    page.fill("#c-keys", "")
    page.click("#create")
    page.locator("#created .banner").wait_for()
    created = page.locator("#created").inner_text()
    print("created:", created)
    assert "Created table" in created
    page.screenshot(path="mock_data/combine_created.png")
    new_id = int(created.split("#")[1].split()[0])
    snap = api.get(f"/api/tables/{new_id}/snapshot").json()
    assert snap["origin"]["kind"] == "combined" and len(snap["origin"]["inputs"]) == 2, snap["origin"]
    page.goto(f"{BASE}/ui/profiler/{new_id}")
    page.locator("#run-profile").wait_for()
    browser.close()

print("console errors:", errors)
sys.exit(1 if errors else 0)
