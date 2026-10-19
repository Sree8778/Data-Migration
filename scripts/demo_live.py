"""LIVE DEMO: drives a visible Edge window through a full migration of the COMPLEX dataset, one narrated step at a time.

    python scripts/demo_live.py [--pace 1.0] [--base http://localhost:8000]

The dataset is mock_data/complex/customers_complex.csv: 16,600 rows, 27 cryptic SAP-style columns, cp1252 text with ';' delimiters, persons and
organisations, 13 countries written in several languages, planted duplicates (exact, renamed, typos, e-mail/phone only), look-alikes that must
NOT merge, blanks, unknown lookups, over-long names, deleted and test records. A caption at the top of the console names each step; each action is
highlighted before it is done. --pace 1.5 slows everything down, 0.5 speeds it up. Needs a Gemini key in the server for the AI mapping step.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "mock_data" / "complex"
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--pace", type=float, default=1.0)
args = ap.parse_args()
BASE, PACE = args.base, args.pace
KEY = json.loads((DATA / "answer_key.json").read_text(encoding="utf-8"))
SYSTEM, TABLE = "SAP_ECC_DEMO_" + __import__("time").strftime("%H%M"), "KNA1_COMPLEX"   # fresh system per run so a re-run never collides
TOTAL_STEPS = 18
step_no = 0


def pause(seconds: float = 1.2) -> None:
    time.sleep(seconds * PACE)


def say(page: Page, text: str, detail: str = "") -> None:
    """Show the step caption across the top of the console and print it to the log."""
    global step_no
    step_no += 1
    print(f"[step {step_no}/{TOTAL_STEPS}] {text}" + (f" - {detail}" if detail else ""), flush=True)
    page.evaluate("""([n, total, text, detail]) => {
        let b = document.getElementById('demo-caption');
        if (!b) {
            b = document.createElement('div'); b.id = 'demo-caption';
            const s = b.style;
            s.position = 'fixed'; s.top = '0'; s.left = '50%'; s.transform = 'translateX(-50%)'; s.zIndex = '99999'; s.maxWidth = '70vw';
            s.background = 'rgba(30,27,75,.94)'; s.color = '#fff'; s.padding = '10px 22px'; s.borderRadius = '0 0 10px 10px';
            s.font = '600 16px "Segoe UI", system-ui, sans-serif'; s.boxShadow = '0 6px 22px rgba(0,0,0,.35)'; s.textAlign = 'center'; s.pointerEvents = 'none';
            document.body.appendChild(b);
        }
        b.innerHTML = '';
        const head = document.createElement('div'); head.textContent = 'Step ' + n + ' of ' + total + ': ' + text; b.appendChild(head);
        if (detail) { const d = document.createElement('div'); d.textContent = detail; d.style.fontWeight = '400'; d.style.fontSize = '13px'; d.style.opacity = '.85'; d.style.marginTop = '3px'; b.appendChild(d); }
    }""", [step_no, TOTAL_STEPS, text, detail])
    pause(1.8)


def point(loc, seconds: float = 0.9):
    """Highlight an element so the viewer sees what is about to be used."""
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.highlight()
    except Exception:  # noqa: BLE001
        pass
    pause(seconds)
    return loc


def act_as(page: Page, user: str) -> None:
    box = page.locator("#actor")
    point(box, 0.4)
    box.fill(user)
    box.press("Tab")
    pause(0.6)


def frame(page: Page):
    return page.frame_locator("iframe.active")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=False, slow_mo=int(250 * PACE), args=["--start-maximized"])
    ctx = browser.new_context(no_viewport=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('migration.user', 'demo.author'); localStorage.removeItem('migration.console.tabs');")
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    page.wait_for_timeout(1200)

    # ------------------------------------------------------------------ 1. the console
    say(page, "The Management Center", "Explorer on the left, tabs for each task, status bar below. You are acting as demo.author.")
    point(page.locator("#explorer"), 1.2)
    point(page.locator(".iconbar"), 1.2)
    point(page.locator(".statusbar"), 1.0)

    # ------------------------------------------------------------------ 2. add the source
    say(page, "Add a source connection", "New Source opens the wizard. The legacy file is a 3.7 MB Windows-1252 export of 16,600 customers.")
    point(page.get_by_role("button", name="New Source")).click()
    page.locator("#wizard[open]").wait_for()
    pause(1)
    point(page.locator("#s-name"), 0.5).type(SYSTEM, delay=70)
    pause(0.8)
    say(page, "The provider: an extract file", "Databases (Oracle, SQL Server, PostgreSQL, MySQL), Azure, S3 and SFTP are in this list too.")
    point(page.locator("#s-kind"), 1.0)
    page.locator("#wz-next").click()
    pause(0.8)

    say(page, "Choose the file: the format is detected, not configured", "Watch the encoding, the delimiter and the column count appear.")
    point(page.locator("#p-file"), 0.6)
    page.set_input_files("#p-file", str(DATA / "customers_complex.csv"))
    page.locator("#p-status.ok").wait_for(timeout=60000)
    point(page.locator("#p-status"), 2.2)
    print("   detected:", page.locator("#p-status").inner_text(), flush=True)
    point(page.locator("#p-delimiter"), 1.0)
    point(page.locator("#p-encoding"), 1.0)
    page.locator("#wz-next").click()
    pause(0.8)

    say(page, "Name the table, pick the key, add the control total", "16,600 rows are reported by the source system: the file is rejected if the count differs.")
    name = page.locator("#t-name")
    name.fill("")
    name.type(TABLE, delay=70)
    point(page.locator('.t-key[data-col="KUNNR"]'), 0.8).check()
    point(page.locator("#t-expected"), 0.5).fill(str(KEY["rows"]))
    pause(1.2)
    page.locator("#wz-next").click()
    pause(0.6)
    point(page.locator("#a-profile"), 0.6)
    page.uncheck("#a-studio")                                    # we open the studio ourselves, a little later
    page.locator("#wz-next").click()
    pause(0.8)
    say(page, "Review the summary and finish", "The file is landed as a snapshot with a SHA-256, the rows are counted, the table is registered and profiled.")
    point(page.locator("#wz-body"), 2.0)
    page.locator("#wz-next").click()
    page.locator("#wizard[open]").wait_for(state="detached", timeout=120000)
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for(timeout=60000)

    # ------------------------------------------------------------------ 3. the profile
    say(page, "The profile: what is really in this file", "Null ratios, distinct counts, lengths, top values, health scores and flags for all 27 columns.")
    f = frame(page)
    f.locator("#summary .stat").first.wait_for(timeout=60000)
    point(f.locator("#summary"), 2.0)
    f.locator("#columns table").wait_for()
    point(f.locator("#columns"), 2.0)
    f.locator("#columns").evaluate("el => el.scrollIntoView({block: 'start'})")
    pause(2.5)

    # ------------------------------------------------------------------ 4. AI mapping
    say(page, "Open the Mapping Studio and let the AI propose a mapping", "Gemini reads the cryptic names (KUNNR, NAME1, ORT01...) plus sample values and maps them to SAP fields.")
    node = page.locator(f'#tree li[data-id="sys:{SYSTEM}"] li[data-id^="t"] > .node').first
    point(node, 0.6).click()
    page.get_by_role("button", name="Mapping", exact=True).click()
    page.locator("#tabstrip .tab", has_text="Mapping").wait_for()
    f = frame(page)
    f.locator("#mapping-select").wait_for()
    pause(1.2)
    point(f.locator("#generate"), 0.8).click()
    print("   AI mapping started; this takes about a minute (27 columns, one model call each)", flush=True)
    f.locator("#job:not(.hidden)").wait_for(timeout=30000)
    point(f.locator("#job"), 2.0)
    f.locator("#toast.show", has_text="rules proposed").wait_for(timeout=360000)
    print("   ", f.locator("#toast").inner_text(), flush=True)
    pause(2.0)
    f.locator(".rule").first.wait_for()
    say(page, "The AI's proposal, with confidence and review flags", "Every rule shows its target SAP field, rule type, confidence and whether a human must review it.")
    point(f.locator("#rules"), 1.5)
    f.locator("#rules").evaluate("el => el.scrollIntoView({block: 'start'})")
    pause(2.5)

    # ------------------------------------------------------------------ 5. steward corrections
    say(page, "The steward completes the mapping", "Lookups for countries and payment terms, plus defaults for the mandatory fields the legacy system never had.")

    def edit_rule(target_text: str, lookups: dict) -> bool:
        rule = f.locator(".rule", has_text=target_text).first
        if rule.count() == 0:
            return False
        point(rule, 0.6)
        rule.locator(".edit").click()
        pause(0.6)
        rule.locator('select[name="rule_type"]').select_option("VALUE_MAPPING")
        rule.locator('textarea[name="lookups"]').fill(json.dumps(lookups, ensure_ascii=False))
        pause(1.0)
        rule.locator('input[name="confirm"]').check()
        rule.get_by_role("button", name="Save rule").click()
        f.locator("#toast.show").wait_for()
        pause(1.0)
        return True

    print("   country lookups:", edit_rule("ADRC.COUNTRY", KEY["lookups"]["country"]), "| payment terms lookups:", edit_rule("KNB1.ZTERM", KEY["lookups"]["terms"]), flush=True)
    for column, logic in (("BU_GROUP", "'BP01'"), ("TYPE", "'2'")):
        if f.locator(".rule", has_text=f"BUT000.{column}").count():
            continue
        if f.locator("#add-rule.hidden").count():            # the toggle flips the panel, so only click it when the panel is closed
            point(f.locator("#add-rule-toggle"), 0.5).click()
        form = f.locator("#add-rule-form")
        form.wait_for(state="visible", timeout=10000)
        form.locator('input[name="target_table"]').fill("BUT000")
        form.locator('input[name="target_column"]').fill(column)
        form.locator('select[name="rule_type"]').select_option("STATIC_VALUE")
        form.locator('input[name="transformation_logic"]').fill(logic)
        pause(0.8)
        form.get_by_role("button", name="Add rule").click()
        f.locator(".rule", has_text=f"BUT000.{column}").first.wait_for()
        pause(0.8)

    # ------------------------------------------------------------------ 6. four-eyes approval
    say(page, "Submit the mapping for approval", "demo.author submits it. Nothing can run against the live target until a second person approves.")
    point(f.locator("#submit-mapping"), 0.8).click()
    f.locator("#mapping-status", has_text="SUBMITTED").wait_for()
    pause(1.5)
    say(page, "A different person approves: now acting as demo.steward", "The approval records the approver's name and the time; the creator of a mapping set can never approve it.")
    act_as(page, "demo.steward")
    point(f.locator("#approve-mapping"), 0.8).click()
    f.locator("#mapping-status", has_text="APPROVED").wait_for()
    pause(1.8)

    # ------------------------------------------------------------------ 7. the pipeline
    say(page, "Run the pipeline", "Transform with the approved rules, find duplicates, validate every field against the SAP catalog, record defects.")
    point(f.locator("#run-pipeline"), 0.8).click()
    page.locator("#tabstrip .tab", has_text="Governance").wait_for(timeout=300000)
    f = frame(page)
    f.locator("#gate").wait_for(timeout=60000)
    pause(2.0)

    # ------------------------------------------------------------------ 8. the gate and the defects
    say(page, "The governance gate says BLOCKED, and exactly why", "A real extract has real problems: blank cities and countries, unknown lookup values, names longer than SAP allows.")
    point(f.locator("#gate"), 3.0)
    f.locator("#defects table").wait_for()
    say(page, "Defects, grouped by rule", "Each group names the SAP field, how many records fail, sample keys and a plain-language root cause.")
    f.locator("#defects").evaluate("el => el.scrollIntoView({block: 'start'})")
    point(f.locator("#defects"), 3.5)
    print("   gate:", f.locator("#gate").inner_text().replace("\n", " | ")[:200], flush=True)

    # ------------------------------------------------------------------ 9. data construction + certificate
    say(page, "Data Construction: values the legacy system never had", "Download the template, fill the gaps (like the mandatory reconciliation account), upload, and the run re-validates.")
    point(f.locator("#tab-construction"), 0.8).click()
    pause(2.5)
    point(f.locator("#tab-defects"), 0.5).click()
    pause(0.8)
    say(page, "The reconciliation certificate: every record accounted for", "Extracted, transformed, duplicates removed, rejected, valid: the numbers must add up with zero variance.")
    point(page.get_by_role("button", name="Certificate", exact=True), 0.8).click()
    page.locator("#tabstrip .tab", has_text="Certificate").wait_for()
    f = frame(page)
    pause(4.0)
    point(f.locator("body"), 3.0)

    # ------------------------------------------------------------------ 10. close
    say(page, "That was a complete migration run", "Source landed and verified, AI-mapped, approved by a second person, validated, duplicates found, gate enforced, certificate produced.")
    page.get_by_role("button", name="Start", exact=True).click()
    pause(6)
    print("\nconsole errors:", [e for e in errors if "422" not in e], flush=True)
    browser.close()
print("DEMO COMPLETE")
