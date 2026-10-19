"""Edge/Playwright check of 'Rows to leave out' in the Mapping Studio, and of what the certificate then says.

    python scripts/e2e_browser_exclusions.py [--base http://localhost:8000]

Registers its own small legacy table (deleted-flag records and test customers among normal ones), then through the UI: reads the suggestions
the data makes, uses one, previews and adds a manual rule, deletes one; runs the pipeline and opens the certificate to check that the rows
left out are their own line and the variance is still zero. Exit 1 on a failed assertion or an unexpected console error.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
args = ap.parse_args()
BASE = args.base
api = httpx.Client(base_url=BASE, headers={"X-User": "excl.tester"}, timeout=180)

NL = chr(10)
rows = ["ID,NAME1,CITY,LOEVM"]
for i in range(1, 61):
    flag = "X" if i % 10 == 0 else ""                                   # 6 deleted
    name = {7: "TEST CUSTOMER 7", 17: "Dummy Corp", 27: "DO NOT USE 27"}.get(i, f"Customer {i} GmbH")  # 3 test records
    rows.append(f"{i},{name},{'Berlin' if i % 2 else 'Paris'},{flag}")
suffix = time.strftime("%H%M%S")
registered = api.post("/api/sources/register", data={"system_name": f"EXCL_{suffix}", "table_name": "CUSTOMERS", "primary_key_columns": "ID"},
                      files={"file": ("customers.csv", NL.join(rows).encode(), "text/csv")})
assert registered.status_code == 201, registered.text
table = registered.json()["table_id"]
api.post(f"/api/profile/{table}").raise_for_status()
mset = api.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
assert api.post(f"/api/mapping/{mset}/rules", json={"target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "DIRECT_COPY", "source_column": "NAME1"}).status_code == 201

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.add_init_script("localStorage.setItem('migration.user', 'excl.tester')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{BASE}/ui/studio/{table}")
    page.locator("#mapping-select").wait_for()
    page.select_option("#mapping-select", str(mset))
    page.locator(".rule").first.wait_for()

    # ---- the data's own suggestions
    page.locator("#excl-suggest .excl-use").first.wait_for()
    suggestions = page.locator("#excl-suggest .rule").all_inner_texts()
    print("suggestions:", [s.replace(NL, " ")[:90] for s in suggestions])
    assert any("LOEVM" in s and "6 row(s)" in s for s in suggestions), suggestions        # the deletion flag is set on 6 rows
    assert any("NAME1" in s and "3 row(s)" in s for s in suggestions), suggestions        # three names say TEST / DUMMY / DO NOT USE
    assert page.locator("#excl-summary").inner_text().strip().startswith("- none")

    # ---- use the deletion-flag suggestion: the form is filled and the match count shown
    page.locator("#excl-suggest .rule", has_text="LOEVM").locator(".excl-use").click()
    page.locator("#excl-preview", has_text="6 row(s) match").wait_for()
    print("form after Use this:", page.locator("#excl-column").input_value(), page.locator("#excl-operator").input_value(), repr(page.locator("#excl-value").input_value()), repr(page.locator("#excl-reason").input_value()[:40]))
    assert page.locator("#excl-column").input_value() == "LOEVM" and page.locator("#excl-value").input_value() == "X"
    page.locator("#excl-add").click()
    page.locator("#toast.show", has_text="6 row(s) will be left out").wait_for()
    page.locator('#excl-rules [data-excl="1"]').wait_for()
    assert page.locator("#excl-summary").inner_text().strip().startswith("- 1 rule(s), 6 of 60 rows left out")
    page.locator("#excl-suggest .rule", has_text="LOEVM").wait_for(state="detached")     # an accepted suggestion is not offered again

    # ---- a manual rule: values of 'in' are comma separated
    page.select_option("#excl-column", "NAME1")
    page.select_option("#excl-operator", "in")
    page.fill("#excl-value", "TEST CUSTOMER 7, Dummy Corp")
    page.fill("#excl-reason", "test customers created by the migration team")
    page.click("#excl-check")
    page.locator("#excl-preview", has_text="2 row(s) match").wait_for()
    page.locator("#excl-add").click()
    page.locator('#excl-rules [data-excl="2"]').wait_for()
    assert page.locator("#excl-summary").inner_text().strip().startswith("- 2 rule(s), 8 of 60 rows left out")
    page.screenshot(path="mock_data/studio_exclusions.png")

    # ---- a rule without a reason is not added
    page.fill("#excl-value", "DO NOT USE 27")
    page.fill("#excl-reason", "")
    page.click("#excl-add")
    assert page.locator('#excl-rules [data-excl="3"]').count() == 0

    # ---- run, then the certificate says what was left out
    run = api.post(f"/api/pipeline/run/{mset}", json={"deduplicate": False}).json()
    assert run["run"]["source_records"] == 60 and run["run"]["excluded_records"] == 8, run["run"]
    page.goto(f"{BASE}/ui/certificate/{run['run_id']}")
    page.locator("text=EXCLUDED_BY_RULE").first.wait_for(timeout=30000)
    body = page.locator("body").inner_text()
    assert "test customers created by the migration team" in body and "rows_left_out_are_kept_with_their_reason" in body, body[:400]
    print("certificate shows the exclusion line and the check")

    # ---- delete a rule: back to one
    page.goto(f"{BASE}/ui/studio/{table}")
    page.locator("#mapping-select").wait_for()
    page.select_option("#mapping-select", str(mset))
    page.locator('#excl-rules [data-excl="2"]').wait_for()
    page.locator('#excl-rules [data-excl="2"] .excl-delete').click()
    page.locator('#excl-rules [data-excl="2"]').wait_for(state="detached")
    assert page.locator("#excl-summary").inner_text().strip().startswith("- 1 rule(s), 6 of 60 rows left out")
    browser.close()

print("console errors:", errors)
sys.exit(1 if errors else 0)
