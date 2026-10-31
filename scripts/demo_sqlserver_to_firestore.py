"""LIVE DEMO: a SQL Server table migrated through the whole pipeline and DELIVERED INTO GOOGLE FIREBASE (Firestore), in a visible Edge window with a glowing cursor.

    python scripts/demo_sqlserver_to_firestore.py --key-file C:\\path\\firebase-key.json [--pace 1.0] [--base http://localhost:8770] [--headless] [--stop-after N]

What happens, one captioned step at a time: the SQL Server source is added in the wizard (read-only login), landed and profiled; the SAP mapping is built in the Mapping
Studio; a second person approves it; the pipeline transforms, de-duplicates and validates; the governance gate opens; a steward signs off; the Firebase target is added in the
New Target wizard (service-account key loaded from a file); the validated records are DELIVERED as documents, counted and compared value by value, and verified again; the
reconciliation certificate lists the delivery. A panel shows documents read straight from Firebase through the Firestore API.

Needs: the application running with the code that has the Firestore target; the demo SQL Server (docker container erp_sqlserver) with the view dbo.V_AR_DEMO and the read-only
login demo_reader; a service-account key for a scratch Firebase project (writes a collection into it; roll the delivery back from the Governance hub when you are done).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8770")
ap.add_argument("--pace", type=float, default=1.0)
ap.add_argument("--key-file", required=True)
ap.add_argument("--collection", default="BP_FROM_SQLSERVER")
ap.add_argument("--sql-host", default="localhost")
ap.add_argument("--sql-port", default="11433")
ap.add_argument("--sql-db", default="legacy_erp")
ap.add_argument("--sql-user", default="demo_reader")
ap.add_argument("--sql-password", default="Demo_ro_Pw_1!")
ap.add_argument("--sql-view", default="V_AR_DEMO")
ap.add_argument("--headless", action="store_true")
ap.add_argument("--stop-after", type=int, default=0, help="stop after this step number (for building the script)")
ap.add_argument("--resume", type=int, default=0, help="skip the first N parts (1 = source imported, 2 = + Firebase target added, 3 = + mapping built): for building the script")
ap.add_argument("--rollback", action="store_true", help="roll the delivery back at the end (rehearsals); the real run leaves the documents in Firebase to look at")
ap.add_argument("--narrate", action="store_true", help="speak the explanation with a neural voice (needs internet for the text-to-speech) and keep the timeline for scripts/narrate_video.py")
ap.add_argument("--voice", default="en-US-AvaMultilingualNeural")
ap.add_argument("--timeline", default="", help="where to write the timeline of the steps")
args = ap.parse_args()
ROOT = Path(__file__).resolve().parent.parent
BASE, PACE = args.base, args.pace
KEY = json.loads(Path(args.key_file).read_text(encoding="utf-8"))
SYSTEM = "LEGACY_ERP_SQLSERVER"
TARGET = "FIREBASE_TARGET"
TOTAL_STEPS = 24 + (1 if args.rollback else 0)

COUNTRY_LK = {c: c for c in ("DE", "FR", "US", "IT", "GB", "ES", "PT", "AT", "CH", "JP", "BR")}
TERMS_LK = {"NET30": "NT30", "NET60": "NT60", "COD": "CASH", "2/10 NET 30": "NT3D"}
# (target table, target column, rule type, source column, expression / literal)
RULES = [
    ("BUT000", "NAME_ORG1", "DIRECT_COPY", "NAME1", ""), ("BUT000", "NAME_ORG2", "DIRECT_COPY", "NAME2", ""), ("BUT000", "BP_EXT", "DIRECT_COPY", "CUST_ID", ""),
    ("BUT000", "TYPE", "STATIC_VALUE", "", "'2'"), ("BUT000", "BU_GROUP", "STATIC_VALUE", "", "'Z001'"),
    ("BUT000", "BU_SORT1", "SQL_EXPRESSION", "NAME1", "upper(left(trim(NAME1), 20))"),
    ("ADRC", "STREET", "DIRECT_COPY", "STREET", ""), ("ADRC", "HOUSE_NUM1", "DIRECT_COPY", "HOUSE_NO", ""), ("ADRC", "CITY1", "DIRECT_COPY", "CITY", ""),
    ("ADRC", "POST_CODE1", "DIRECT_COPY", "POSTAL", ""), ("ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", ""), ("ADRC", "LANGU", "DIRECT_COPY", "LANG", ""),
    ("ADR6", "SMTP_ADDR", "DIRECT_COPY", "EMAIL", ""), ("ADR2", "TEL_NUMBER", "DIRECT_COPY", "PHONE", ""), ("KNB1", "ZTERM", "VALUE_MAPPING", "PAY_TERMS", ""),
    ("KNVV", "WAERS", "DIRECT_COPY", "CURRENCY", ""),
]
LOOKUPS = {"COUNTRY": COUNTRY_LK, "ZTERM": TERMS_LK}


# ====================================================================== the show: the shared kit (explanation in its own window, thin outline, glowing cursor)
from demo_kit import Kit  # noqa: E402

kit = Kit(PACE, TOTAL_STEPS, args.stop_after)
pause, say, point, click, type_into, act_as, frame = kit.pause, kit.say, kit.point, kit.click, kit.type_into, kit.act_as, kit.frame
banner, clear_banner = kit.banner, kit.clear_banner


def run() -> None:
    if args.narrate:
        import demo_narration
        print("preparing the voice-over (the script text is sent to Microsoft's speech service) ...", flush=True)
        kit.narration = demo_narration.prepare(demo_narration.sqlserver_to_firebase_script(), ROOT / "mock_data" / "recordings" / "_voice_cache", args.voice)
        print(f"voice-over ready: {sum(v['duration'] for v in kit.narration.values()):.0f} s of speech in {len(kit.narration)} steps", flush=True)
    with sync_playwright() as p:
        browser, page, errors = kit.open(p, args.headless)
        page.goto(BASE + "/")
        page.locator("#tree .node").first.wait_for()
        page.wait_for_timeout(1000)
        print("SCREEN_START", flush=True)             # a screen recording starts here (scripts/record_screen.py --start-marker): the windows are open and the application is on screen
        time.sleep(2.5)                                 # the recorder needs a moment to start; the first caption must be in the video
        if args.resume < 1:
            part1(page)
        else:
            kit.step_no = 6
        if args.resume < 2:
            part_target(page)
        else:
            kit.step_no = 9
        if args.resume < 3:
            part2(page)
        else:
            kit.step_no = 14
        part3(page)
        kit.finish()
        if args.narrate:
            kit.write_timeline(args.timeline or ROOT / "mock_data" / "recordings" / "_voice_cache" / "timeline_sqlserver_to_firebase.json")
        print("\nconsole errors:", [e for e in errors if "422" not in e], flush=True)
        print("SCREEN_END", flush=True)               # ... and stops here, while the windows are still open
        time.sleep(1.5)
        browser.close()
    print("DEMO COMPLETE")


def part1(page: Page) -> None:
    # ------------------------------------------------------------------ 1. the console
    say(page, "The Management Center", "A legacy SQL Server database is about to be migrated, cleaned, de-duplicated and delivered into Google Firebase.")
    point(page, page.locator("#explorer"), 1.0)
    point(page, page.locator(".iconbar"), 0.8)

    # ------------------------------------------------------------------ 2. the source
    say(page, "Add the SQL Server source", "A read-only login: the application can look at the legacy data but cannot change it.")
    click(page, page.get_by_role("button", name="New Source"))
    page.locator("#wizard[open]").wait_for()
    pause(0.8)
    type_into(page, page.locator("#s-name"), SYSTEM)
    point(page, page.locator("#s-kind"), 0.8)
    page.select_option("#s-kind", "mssql")
    pause(0.6)
    click(page, page.locator("#wz-next"))
    type_into(page, page.locator("#c-host"), args.sql_host, 35)
    type_into(page, page.locator("#c-port"), args.sql_port, 35)
    type_into(page, page.locator("#c-database"), args.sql_db, 35)
    type_into(page, page.locator("#c-user"), args.sql_user, 35)
    type_into(page, page.locator("#c-password"), args.sql_password, 35)
    say(page, "Test the connection", "The wizard connects for real and counts what the account can see. The password is kept in server memory only.")
    click(page, page.locator("#c-test"))
    page.locator("#c-status.ok").wait_for(timeout=60000)
    point(page, page.locator("#c-status"), 1.6)
    print("   ", page.locator("#c-status").inner_text(), flush=True)
    click(page, page.locator("#wz-next"))

    say(page, "Pick the table to bring in", "A view of 1,602 customers. Each table is landed as a snapshot and its row count is checked against the source.")
    page.locator("#d-tree .wz-schema").first.wait_for()
    box = page.locator(f'input[data-table="{args.sql_view}"]')
    if box.count() == 0:
        click(page, page.locator('[data-toggle="dbo"]'))
        box.wait_for()
    point(page, page.locator(f'#d-tree .tbl-name[data-table="{args.sql_view}"]'), 0.6).click()
    page.locator("#d-cols", has_text="columns").wait_for()
    point(page, page.locator("#d-cols"), 1.8)
    others = page.locator(f'#d-tree input[type=checkbox]:checked:not([data-table="{args.sql_view}"])')      # the setup guide may have ticked more than we want
    check = page.locator(f'input[data-table="{args.sql_view}"]')
    if not check.is_checked():
        click(page, check)
    while others.count():
        others.first.uncheck()
    click(page, page.locator("#wz-next"))
    point(page, page.locator("#a-profile"), 0.5)
    page.uncheck("#a-studio")
    click(page, page.locator("#wz-next"))
    say(page, "Review and finish", "The rows are read, counted against the database's own count, stored as a snapshot and profiled.")
    point(page, page.locator("#wz-body"), 1.6)
    click(page, page.locator("#wz-next"))
    page.locator("#wizard[open]").wait_for(state="detached", timeout=180000)
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for(timeout=60000)

    # ------------------------------------------------------------------ 3. the profile
    say(page, "The profile: what is really in this table", "Null ratios, distinct counts, lengths and top values for all 17 columns.")
    f = frame(page)
    f.locator("#summary .stat").first.wait_for(timeout=60000)
    point(page, f.locator("#summary"), 1.8)
    f.locator("#columns table").wait_for()
    point(page, f.locator("#columns"), 2.0)


def open_studio(page: Page):
    node = page.locator(f'#tree li[data-id="sys:{SYSTEM}"] li[data-id^="t"] > .node').first
    if not node.is_visible():                                  # the tree is collapsed (a page opened fresh): open the system first
        click(page, page.locator(f'#tree li[data-id="sys:{SYSTEM}"] > .node .twist').first, 0.4)
    click(page, node)
    click(page, page.get_by_role("button", name="Mapping", exact=True))
    page.locator("#tabstrip .tab", has_text="Mapping").wait_for()
    f = frame(page)
    f.locator("#mapping-select").wait_for()
    return f


def part_target(page: Page) -> None:
    # ------------------------------------------------------------------ the destination
    say(page, "Add the destination: Google Firebase", "New Target. Firebase is a document database: every validated customer will become one document.")
    click(page, page.get_by_role("button", name="New Target"))
    page.locator("#wizard[open]").wait_for()
    page.select_option("#g-type", "db")
    type_into(page, page.locator("#g-name"), TARGET)
    point(page, page.locator("#g-dialect"), 0.8)
    page.select_option("#g-dialect", "firestore")
    pause(0.8)
    click(page, page.locator("#wz-next"))

    say(page, "Load the service-account key", "A key file from the Google Cloud console. It is read in the browser and kept in the server's memory only: never saved, never shown again.")
    point(page, page.locator("#c-keyfile"), 0.8)
    page.set_input_files("#c-keyinput", args.key_file)
    expect(page.locator("#c-database")).to_have_value(KEY["project_id"])
    point(page, page.locator("#c-database"), 1.0)
    point(page, page.locator("#c-user"), 1.0)
    point(page, page.locator("#c-password"), 1.0)           # masked: the key is shown as dots
    click(page, page.locator("#c-test"))
    page.locator("#c-status.ok").wait_for(timeout=90000)
    point(page, page.locator("#c-status"), 1.6)
    print("   ", page.locator("#c-status").inner_text(), flush=True)
    click(page, page.locator("#wz-next"))

    say(page, "Prove the account can write", "The wizard writes one test document, reads it back and deletes it. Delivery is refused if this fails.")
    page.locator("#t-schema option", has_text="(default)").wait_for(state="attached")
    page.select_option("#t-schema", "(default)")
    pause(0.6)
    click(page, page.locator("#t-test"))
    page.locator("#t-status.ok").wait_for(timeout=90000)
    point(page, page.locator("#t-status"), 1.6)
    print("   ", page.locator("#t-status").inner_text(), flush=True)
    click(page, page.locator("#wz-next"))
    click(page, page.locator("#wz-next"))                      # the options page, then the summary
    point(page, page.locator("#wz-body"), 1.6)
    click(page, page.locator("#wz-next"))                      # Finish
    page.locator("#wizard[open]").wait_for(state="detached", timeout=60000)
    pause(1.0)


def part2(page: Page) -> None:
    # ------------------------------------------------------------------ 4. the mapping
    say(page, "Open the Mapping Studio", "The legacy columns on one side, the SAP business-partner fields on the other. With a Gemini key the AI proposes the mapping; here it is built by hand.")
    f = open_studio(page)
    pause(1.0)
    click(page, f.locator("#new-set"))
    f.locator("#mapping-status", has_text="DRAFT").wait_for(timeout=30000)
    pause(0.8)

    say(page, "Point this mapping at Firebase", "The destination is chosen with the mapping, so Firebase's own rules (keys that can become document IDs, the free plan's quota) are checked before anybody approves.")
    point(page, f.locator("#target-select"), 0.8)
    f.locator("#target-select").select_option(label=f.locator("#target-select option", has_text=TARGET).first.inner_text())
    f.locator("#toast.show", has_text="Destination").wait_for(timeout=30000)
    point(page, f.locator("#target-select"), 1.4)

    say(page, "Map the legacy columns to SAP fields", "Each rule says where an SAP field gets its value: copied, computed, a constant, or translated through a lookup table.")
    for n, (table, column, rtype, source, logic) in enumerate(RULES, 1):
        if f.locator("#add-rule.hidden").count():
            click(page, f.locator("#add-rule-toggle"), 0.3)
        form = f.locator("#add-rule-form")
        form.wait_for(state="visible", timeout=10000)
        form.locator('input[name="target_table"]').fill(table)
        form.locator('input[name="target_column"]').fill(column)
        form.locator('select[name="rule_type"]').select_option(rtype)
        form.locator('input[name="source_column"]').fill(source)
        form.locator('input[name="transformation_logic"]').fill(logic)
        point(page, form, 0.35)
        click(page, form.get_by_role("button", name="Add rule"), 0.25)
        f.locator(".rule", has_text=f"{table}.{column}").first.wait_for(timeout=15000)
        if n % 4 == 0:
            print(f"   {n} of {len(RULES)} rules added", flush=True)
    point(page, f.locator("#rules"), 1.2)

    say(page, "Lookup tables for the two coded fields", "Country and payment terms are translated: the legacy code NET60 becomes the SAP key NT60.")
    for column, lookups in (("COUNTRY", COUNTRY_LK), ("ZTERM", TERMS_LK)):
        rule = f.locator(".rule", has_text=f".{column}").first
        point(page, rule, 0.5)
        click(page, rule.locator(".edit"), 0.4)
        rule.locator('select[name="rule_type"]').select_option("VALUE_MAPPING")
        rule.locator('textarea[name="lookups"]').fill(json.dumps(lookups))
        point(page, rule.locator('textarea[name="lookups"]'), 0.8)
        rule.locator('input[name="confirm"]').check()
        click(page, rule.get_by_role("button", name="Save rule"), 0.4)
        f.locator("#toast.show").wait_for()
        pause(0.8)

    say(page, "Check the rules on a sample of the real rows", "A dry run before anybody is asked to approve: how many sampled rows pass the SAP field checks?")
    click(page, f.locator("#preflight"))
    f.locator("#preflight-report, #preflight-panel, .preflight").first.wait_for(timeout=120000)
    pause(1.5)
    f.locator("#preflight-panel .destination").wait_for(timeout=30000)
    point(page, f.locator("#preflight-panel .destination"), 3.0)                  # what Firebase says about these records, before anybody approves
    print("   destination:", f.locator("#preflight-panel .destination").inner_text().replace(chr(10), " | ")[:200], flush=True)


def firebase_documents(limit: int = 3) -> tuple[int, list[str]]:
    """Read straight from Firebase through the Firestore API (not through the application): how many documents the collection holds, and the first few."""
    from google.cloud import firestore
    from google.oauth2 import service_account
    db = firestore.Client(project=KEY["project_id"], credentials=service_account.Credentials.from_service_account_info(KEY, scopes=["https://www.googleapis.com/auth/datastore"]))
    coll = db.collection(args.collection)
    total = int(coll.count().get()[0][0].value)
    shown = []
    for snap in coll.order_by("__name__").limit(limit).stream():
        d = snap.to_dict()
        shown.append(f"document {snap.id}")
        for k in ("BUT000_NAME_ORG1", "ADRC_CITY1", "ADRC_COUNTRY", "KNB1_ZTERM", "ADR6_SMTP_ADDR", "MIG_RUN_ID"):
            if k in d:
                shown.append(f"    {k}: {d[k]!r}")
    return total, shown


def part3(page: Page) -> None:
    f = frame(page) if page.locator("#tabstrip .tab", has_text="Mapping").count() else open_studio(page)
    if page.locator("#tabstrip .tab.active", has_text="Mapping").count() == 0:
        f = open_studio(page)

    # ------------------------------------------------------------------ approval and run
    say(page, "Submit the mapping for approval", "demo.author built it and submits it. Nothing runs until a second person approves.")
    click(page, f.locator("#submit-mapping"))
    f.locator("#mapping-status", has_text="SUBMITTED").wait_for(timeout=30000)
    pause(1.2)
    say(page, "A different person approves: now acting as demo.steward", "The approval records who and when. The author of a mapping can never approve it.")
    act_as(page, "demo.steward")
    click(page, f.locator("#approve-mapping"))
    f.locator("#mapping-status", has_text="APPROVED").wait_for(timeout=30000)
    pause(1.2)
    say(page, "Run the pipeline", "Transform with the approved rules, find duplicate customers, check every field against the SAP catalogue.")
    click(page, f.locator("#run-pipeline"))
    page.locator("#tabstrip .tab", has_text="Governance").wait_for(timeout=300000)
    f = frame(page)
    f.locator("#gate").wait_for(timeout=60000)
    pause(1.5)

    # ------------------------------------------------------------------ the gate and the sign-off
    say(page, "The governance gate", "It opens only when no critical defect is left. The sign-off is a separate act by a steward.")
    point(page, f.locator("#gate"), 2.5)
    print("   gate:", f.locator("#gate").inner_text().replace("\n", " | ")[:220], flush=True)
    f.locator("#defects table").first.wait_for(timeout=15000) if f.locator("#defects table").count() else None
    click(page, f.locator("#sign-off"))
    f.locator("#toast.show", has_text="signed off by demo.steward").wait_for(timeout=30000)      # not just any toast: an error would show one too
    pause(1.6)
    point(page, f.locator("#gate"), 1.5)
    print("   after sign-off:", f.locator("#gate").inner_text().replace("\n", " | ")[:200], flush=True)

    # ------------------------------------------------------------------ the delivery into Firebase
    say(page, "Deliver the validated customers into Firebase", "Same gate as every load. Choose the Firebase target and name the new collection.")
    point(page, f.locator("#db-delivery"), 0.8)
    f.locator("#db-conn option", has_text=TARGET).wait_for(state="attached", timeout=30000)
    expect(f.locator("#db-conn")).to_have_value(TARGET, timeout=30000)               # already chosen: it is the destination the mapping was pointed at
    point(page, f.locator("#db-conn"), 1.0)
    type_into(page, f.locator("#db-table"), args.collection)
    click(page, f.locator("#db-preview"))
    f.locator("#db-ddl:not(.hidden)").wait_for(timeout=60000)
    point(page, f.locator("#db-ddl"), 2.0)
    f.locator("#db-checks:not(.hidden)").wait_for(timeout=30000)
    point(page, f.locator("#db-checks"), 2.4)                                       # the destination's own checks, with this run's exact numbers
    print("   destination checks:", f.locator("#db-checks").inner_text().replace(chr(10), " | ")[:200], flush=True)
    print("   preview:", f.locator("#db-ddl").inner_text().splitlines()[0][:160], flush=True)
    say(page, "Deliver", "The documents are written, then counted and compared value by value against what was sent. Any difference removes them again.")
    click(page, f.locator("#db-load"))
    f.locator("#db-history table").wait_for(timeout=600000)
    pause(1.0)
    point(page, f.locator("#db-history"), 2.8)
    print("   delivery:", f.locator("#db-history tbody tr").first.inner_text().replace("\n", " | ").replace("\t", " ")[:260], flush=True)

    # ------------------------------------------------------------------ what is really in Firebase
    say(page, "What is really in Firebase now", "Read straight from Google through the Firestore API, not through this application.")
    total, lines = firebase_documents(2)
    banner(page, f"Firebase project {KEY['project_id']}  >  collection {args.collection}  >  {total:,} documents", lines)
    print(f"   firebase holds {total} documents in {args.collection}", flush=True)
    pause(5.0)
    clear_banner(page)
    say(page, "Verify against the live collection", "Every value in Firebase is read again and compared with the validated records. An edit made in the Firebase console would show up here.")
    click(page, f.locator(".db-verify").first)
    f.locator("#toast.show", has_text="identical").wait_for(timeout=300000)
    point(page, f.locator("#toast"), 2.0)
    print("   verify:", f.locator("#toast").inner_text()[:160], flush=True)
    f.locator("#db-history .badge", has_text="still identical").first.wait_for(timeout=30000)
    point(page, f.locator("#db-history"), 1.6)

    # ------------------------------------------------------------------ the certificate
    say(page, "The reconciliation certificate", "Every record accounted for, and the Firebase delivery is a line on it: how many documents, and that the values were compared.")
    click(page, page.get_by_role("button", name="Certificate", exact=True))
    page.locator("#tabstrip .tab", has_text="Certificate").wait_for(timeout=30000)
    cf = frame(page)
    pause(3.0)
    point(page, cf.locator("body"), 2.5)
    page.keyboard.press("End")
    pause(2.0)

    if args.rollback:
        say(page, "Roll the delivery back", "Exactly this run's documents are deleted again; the reason is kept in the record.")
        page.locator("#tabstrip .tab", has_text="Governance").click()
        f = frame(page)
        click(page, f.locator(".db-rollback").first)
        f.locator("#db-history .badge", has_text="rolled back").first.wait_for(timeout=300000)
        point(page, f.locator("#db-history"), 2.0)
        print("   firebase now holds", firebase_documents(0)[0], "documents", flush=True)
    say(page, "A SQL Server table, migrated into Google Firebase", "Read-only source, approved mapping, duplicates merged, gate and sign-off, documents counted and compared in Firebase, certificate issued.")
    pause(4.0)


if __name__ == "__main__":
    run()
