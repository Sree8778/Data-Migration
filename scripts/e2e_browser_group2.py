"""Edge/Playwright check of the AI assistant screens, with REAL Gemini (the server must run with a key):

    python scripts/e2e_browser_group2.py [--base http://localhost:8000]

On the mock legacy extract (country written many ways), in the browser:
  1  Studio: "Suggest rule" on the unmapped COUNTRY column -> the AI's suggestion with its measured evidence -> accept it as a rule that needs a review
  2  make it a value mapping, "Draft lookups with AI" -> the table of proposals -> apply the ticked ones -> the rule now has lookups
  3  "Ask about this mapping" -> an answer, and what it looked at
  4  run the pipeline, open the Governance page: "Ask about this run" -> an answer with a table of records
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
api = httpx.Client(base_url=BASE, headers={"X-User": "g2.ui"}, timeout=600)

rows = list(csv.DictReader(open(ROOT / "mock_data" / "legacy_ar_customers.csv", newline="", encoding="utf-8")))[:600]
buf = io.StringIO()
w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\r\n")
w.writeheader(); w.writerows(rows)
api.post("/api/admin/seed-sap-catalog")
reg = api.post("/api/sources/register", data={"system_name": f"G2_UI_{time.strftime('%H%M%S')}", "table_name": "AR_CUSTOMERS", "primary_key_columns": "CUST_ID"},
               files={"file": ("ar.csv", buf.getvalue().encode("utf-8"))})
assert reg.status_code == 201, reg.text
table = reg.json()["table_id"]
api.post(f"/api/profile/{table}").raise_for_status()
mset = api.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
for tt, tc, src in (("BUT000", "NAME_ORG1", "CUST_NAME"), ("ADRC", "CITY1", "CITY"), ("ADRC", "POST_CODE1", "POSTAL_CODE")):
    api.post(f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": "DIRECT_COPY", "source_column": src}).raise_for_status()

errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.add_init_script("localStorage.setItem('migration.user', 'g2.ui')")
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{BASE}/ui/studio/{table}")
    page.locator("#mapping-select").wait_for()
    page.select_option("#mapping-select", str(mset))
    page.locator("#rules .rule").first.wait_for()

    # ---- 1. suggest a rule for the unmapped COUNTRY column
    page.locator('.legacy-col[data-col="COUNTRY"] .suggest').click()
    page.fill("#sg-hint", "the customer's country, written in many different ways")
    page.click("#sg-run")
    page.locator("#sg-result .rule").wait_for(timeout=180000)
    suggestion = page.locator("#sg-result").inner_text()
    print("suggestion:", suggestion.replace("\n", " ")[:260])
    assert "ADRC.COUNTRY" in suggestion and "Measured on your file" in suggestion
    page.screenshot(path="mock_data/studio_suggest_rule.png")
    page.click("#sg-accept")
    page.locator("#toast.show", has_text="Rule added for COUNTRY").wait_for()
    page.locator("#rules .rule", has_text="ADRC.COUNTRY").first.wait_for()
    card = page.locator("#rules .rule", has_text="ADRC.COUNTRY").first
    assert "AI-suggested" in card.inner_text() and "Needs review" in card.inner_text()
    print("accepted: the new rule is AI-suggested and needs a review")

    # ---- 2. make it a value mapping and draft its lookups
    card.locator(".edit").click()
    editor = card.locator(".rule-edit")
    editor.locator('select[name="rule_type"]').select_option("VALUE_MAPPING")
    editor.locator('input[name="confirm"]').check()
    editor.get_by_role("button", name="Save rule").click()
    page.locator("#toast.show", has_text="Rule updated").wait_for()
    page.wait_for_timeout(1500)                                   # the list reloads after the toast: let it settle before opening the editor again
    card = page.locator("#rules .rule", has_text="ADRC.COUNTRY").first
    card.locator(".edit").click()
    card.locator(".draft-lookups").click()
    page.locator("table.draft").wait_for(timeout=240000)
    ticked = page.locator("table.draft .pick:checked").count()
    total = page.locator("table.draft tbody tr").count()
    print(f"draft: {total} values, {ticked} pre-ticked;", page.locator(".lookup-draft p").nth(1).inner_text()[:200].replace("\n", " "))
    assert total >= 3 and ticked >= 1 and "built-in" in page.locator(".lookup-draft").inner_text()
    page.screenshot(path="mock_data/studio_lookup_draft.png")
    page.locator(".apply-draft").click()
    page.locator("#toast.show", has_text="lookup(s) written").wait_for()
    print("applied:", page.locator("#toast").inner_text())
    import re
    done = page.locator("#rules .rule", has_text=re.compile(r"[1-9]\d* lookup values"))
    done.first.wait_for(timeout=20000)                           # the list reloads after the toast
    meta = done.first.locator(".rule-meta").inner_text()
    assert "approved from an AI draft" in meta and "g2.ui" in meta, meta
    print("the rule now shows:", " ".join(meta.split())[:120])

    # ---- 3. ask about the mapping
    page.fill("#ask-question", "Which mapping rules still need a review?")
    page.click("#ask-go")
    page.locator("#ask-result .answer").wait_for(timeout=120000)
    answer = page.locator("#ask-result .answer").inner_text()
    print("assistant (studio):", answer.replace("\n", " ")[:300])
    assert len(answer) > 20 and page.locator("#ask-result table").count() >= 1
    page.screenshot(path="mock_data/studio_assistant.png")

    # ---- 4. a run, and the assistant on the Governance page
    run = api.post(f"/api/pipeline/run/{mset}", json={"wave_name": "G2_UI"}).json()
    page.goto(f"{BASE}/ui/governance/{run['run_id']}")
    page.locator("#ask-question").wait_for()
    page.fill("#ask-question", "Which rejected records have no country?")
    page.click("#ask-go")
    page.locator("#ask-result .answer").wait_for(timeout=120000)
    answer = page.locator("#ask-result .answer").inner_text()
    print("assistant (governance):", answer.replace("\n", " ")[:300])
    assert len(answer) > 20
    page.screenshot(path="mock_data/governance_assistant.png")
    browser.close()

print("console errors:", errors)
sys.exit(1 if errors else 0)
