"""LIVE DEMO: customers stored in GOOGLE FIREBASE (Firestore) migrated through the whole pipeline and DELIVERED INTO SQL SERVER, in a visible Edge window with a glowing cursor.

    python scripts/demo_firestore_to_sqlserver.py --key-file C:\\path\\firebase-key.json [--pace 1.0] [--base http://localhost:8770] [--headless] [--rollback] [--keep-source]

It first writes a collection of nested customer documents into the Firebase project the key belongs to (a scratch project!), including copies of some customers that only their
e-mail address or tax number gives away. Then, one captioned step at a time: the Firebase source is added in the wizard (service-account key loaded from a file), landed and
profiled; a SQL Server destination is added in the New Target wizard; the SAP mapping reads the nested (dotted) columns such as address.city; the mapping is pointed at the SQL
Server destination, approved by a second person, run (duplicates merged, every field checked), the gate opens, a steward signs off; the validated records are DELIVERED into a new
SQL Server table, counted and compared value by value, verified again; the certificate lists the delivery. A panel shows rows read straight from SQL Server.

Needs: the application running with the Firestore code; the demo SQL Server (docker container erp_sqlserver) with the database FIREBASE_LANDING and the login demo_writer.
The Firebase collection is removed again at the end unless --keep-source is given.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from demo_kit import Kit  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8770")
ap.add_argument("--pace", type=float, default=1.0)
ap.add_argument("--key-file", required=True)
ap.add_argument("--collection", default="crm_customers")
ap.add_argument("--rows", type=int, default=400)
ap.add_argument("--table", default="MIG_FROM_FIREBASE")
ap.add_argument("--sql-host", default="localhost")
ap.add_argument("--sql-port", default="11433")
ap.add_argument("--sql-db", default="FIREBASE_LANDING")
ap.add_argument("--sql-user", default="demo_writer")
ap.add_argument("--sql-password", default="Demo_rw_Pw_1!")
ap.add_argument("--headless", action="store_true")
ap.add_argument("--rollback", action="store_true", help="roll the delivery back at the end (rehearsals); the real run leaves the table in SQL Server to look at")
ap.add_argument("--keep-source", action="store_true", help="leave the demo collection in Firebase")
ap.add_argument("--stop-after", type=int, default=0)
ap.add_argument("--narrate", action="store_true", help="speak the explanation with a neural voice (needs internet for the text-to-speech) and keep the timeline for scripts/narrate_video.py")
ap.add_argument("--voice", default="en-US-AvaMultilingualNeural")
ap.add_argument("--timeline", default="", help="where to write the timeline of the steps (default: next to the voice-over cache)")
args = ap.parse_args()
BASE = args.base
KEY = json.loads(Path(args.key_file).read_text(encoding="utf-8"))
SYSTEM, TARGET = "FIREBASE_CRM", "SQLSERVER_TARGET"
kit = Kit(args.pace, 25 if args.rollback else 24, args.stop_after)
say, point, click, type_into, frame, pause = kit.say, kit.point, kit.click, kit.type_into, kit.frame, kit.pause

COUNTRY_LK = {c: c for c in ("DE", "AT", "CH", "FR", "IT", "ES", "GB")}
TERMS_LK = {"NET30": "NT30", "NET60": "NT60", "COD": "CASH"}
# (target table, target column, rule type, source column, expression / literal): the source columns are Firestore's nested fields, flattened with a dot
RULES = [
    ("BUT000", "NAME_ORG1", "DIRECT_COPY", "name", ""), ("BUT000", "BP_EXT", "DIRECT_COPY", "_id", ""), ("BUT000", "TYPE", "STATIC_VALUE", "", "'2'"),
    ("BUT000", "BU_GROUP", "STATIC_VALUE", "", "'Z001'"), ("BUT000", "BU_SORT1", "SQL_EXPRESSION", "name", 'upper(left(trim("name"), 20))'),
    ("ADRC", "STREET", "DIRECT_COPY", "address.street", ""), ("ADRC", "CITY1", "DIRECT_COPY", "address.city", ""), ("ADRC", "POST_CODE1", "DIRECT_COPY", "address.zip", ""),
    ("ADRC", "COUNTRY", "VALUE_MAPPING", "address.country", ""), ("ADR6", "SMTP_ADDR", "DIRECT_COPY", "contact.email", ""), ("ADR2", "TEL_NUMBER", "DIRECT_COPY", "contact.phone", ""),
    ("KNB1", "ZTERM", "VALUE_MAPPING", "terms", ""),
]
PLACES = [("Köln", "DE", "50667"), ("München", "DE", "80331"), ("Wien", "AT", "1010"), ("Graz", "AT", "8010"), ("Zürich", "CH", "8001"), ("Lyon", "FR", "69001"),
          ("Milano", "IT", "20121"), ("Madrid", "ES", "28001"), ("London", "GB", "EC1A 1BB")]
FIRST = ["Alder", "Birch", "Cedar", "Dune", "Ember", "Fjord", "Garnet", "Harbor", "Iris", "Juniper", "Kestrel", "Lumen", "Maple", "Nimbus", "Orchid"]
KIND = ["Logistik", "Handel", "Bau", "Elektro", "Software", "Consulting", "Metallbau", "Pharma"]


def firestore_client():
    from google.cloud import firestore
    from google.oauth2 import service_account
    return firestore.Client(project=KEY["project_id"], credentials=service_account.Credentials.from_service_account_info(KEY, scopes=["https://www.googleapis.com/auth/datastore"]))


def seed_firebase() -> int:
    """Nested customer documents in the real project: n customers, 12 copies that only the e-mail address gives away, 6 that repeat a tax number."""
    db = firestore_client()
    coll = db.collection(args.collection)
    if any(True for _ in coll.limit(1).stream()):
        raise SystemExit(f"the collection {args.collection} in {KEY['project_id']} already holds documents: remove it, or choose another name with --collection")
    batch, written = db.batch(), 0
    def put(doc_id: str, data: dict) -> None:
        nonlocal batch, written
        batch.set(coll.document(doc_id), data)
        written += 1
        if written % 400 == 0:
            batch.commit()
            batch = db.batch()
    for i in range(args.rows):
        city, country, zip_ = PLACES[i % len(PLACES)]
        put(f"C{i:04d}", {"name": f"{FIRST[i % len(FIRST)]} {KIND[(i // 3) % len(KIND)]} {i:03d} GmbH", "tax_id": f"DE9{i:08d}", "terms": ["NET30", "NET60", "COD"][i % 3],
                          "address": {"street": f"Hauptstrasse {i % 120 + 1}", "city": city, "zip": zip_, "country": country},
                          "contact": {"email": f"accounts{i}@{FIRST[i % len(FIRST)].lower()}{i}.example.com", "phone": f"+49 30 55{i:05d}"}})
    for n in range(12):                                         # another name, another street: only the e-mail address says it is the same customer
        original = 5 + n * 11
        put(f"D{n:04d}", {"name": f"Omega Wholesale {n:02d} Ltd", "terms": "NET30", "address": {"street": f"Side Road {n + 40}", "city": "Bern", "zip": "3011", "country": "CH"},
                          "contact": {"email": f"accounts{original}@{FIRST[original % len(FIRST)].lower()}{original}.example.com"}})
    for n in range(6):                                          # a new name and e-mail, but the same tax number
        original = 20 + n * 17
        put(f"T{n:04d}", {"name": f"Sigma Trading {n:02d} AG", "tax_id": f"DE9{original:08d}", "terms": "COD", "address": {"street": f"Third Lane {n + 7}", "city": "Graz", "zip": "8010", "country": "AT"},
                          "contact": {"email": f"sigma{n}@sigma.example.com"}})
    batch.commit()
    return written


def remove_firebase_collection() -> int:
    coll = firestore_client().collection(args.collection)
    n = 0
    for doc in coll.list_documents():
        doc.delete()
        n += 1
    return n


def sql_connector():
    from src.connectors.base import ConnectionSpec
    from src.connectors.dialects import connector_for
    return connector_for(ConnectionSpec("mssql", args.sql_host, int(args.sql_port), args.sql_db, args.sql_user, args.sql_password, {"connect_timeout": 15}))


def run() -> None:
    if args.narrate:
        import demo_narration
        print("preparing the voice-over (the script text is sent to Microsoft's speech service) ...", flush=True)
        kit.narration = demo_narration.prepare(demo_narration.firebase_to_sqlserver_script(args.rows), ROOT / "mock_data" / "recordings" / "_voice_cache", args.voice)
        print(f"voice-over ready: {sum(v['duration'] for v in kit.narration.values()):.0f} s of speech in {len(kit.narration)} steps", flush=True)
    written = seed_firebase()
    print(f"seeded {written} customer documents into {KEY['project_id']}/{args.collection}", flush=True)
    try:
        with sync_playwright() as p:
            browser, page, errors = kit.open(p, args.headless)
            page.goto(BASE + "/")
            page.locator("#tree .node").first.wait_for()
            page.wait_for_timeout(1000)
            print("SCREEN_START", flush=True)             # a screen recording starts here (scripts/record_screen.py --start-marker): the windows are open and the application is on screen
            time.sleep(2.5)                                 # the recorder needs a moment to start; the first caption must be in the video
            part_source(page, written)
            part_target(page)
            part_mapping(page)
            part_run(page)
            kit.finish()
            if args.narrate:
                kit.write_timeline(args.timeline or ROOT / "mock_data" / "recordings" / "_voice_cache" / "timeline.json")
            print("\nconsole errors:", [e for e in errors if "422" not in e], flush=True)
            print("SCREEN_END", flush=True)               # ... and stops here, while the windows are still open (the clean-up that follows is not recorded)
            time.sleep(1.5)
            browser.close()
    finally:
        if not args.keep_source:
            print(f"removed {remove_firebase_collection()} demo documents from Firebase", flush=True)
    print("DEMO COMPLETE")


def part_source(page: Page, written: int) -> None:
    say(page, "The Management Center", f"{written} customers live in a Google Firebase database. They will be migrated, cleaned, de-duplicated and delivered into SQL Server.")
    point(page, page.locator("#explorer"), 1.0)
    point(page, page.locator(".iconbar"), 0.8)

    say(page, "Add the Firebase source", "Firebase is a document database: every customer is one document, with nested parts such as the address.")
    click(page, page.get_by_role("button", name="New Source"))
    page.locator("#wizard[open]").wait_for()
    pause(0.8)
    type_into(page, page.locator("#s-name"), SYSTEM)
    point(page, page.locator("#s-kind"), 0.8)
    page.select_option("#s-kind", "firestore")
    pause(0.6)
    click(page, page.locator("#wz-next"))

    say(page, "Load the service-account key and test", "A key file from the Google Cloud console, read in the browser, kept in server memory only. The account only needs to read.")
    point(page, page.locator("#c-keyfile"), 0.8)
    page.set_input_files("#c-keyinput", args.key_file)
    expect(page.locator("#c-database")).to_have_value(KEY["project_id"])
    point(page, page.locator("#c-database"), 1.0)
    point(page, page.locator("#c-password"), 1.0)
    click(page, page.locator("#c-test"))
    page.locator("#c-status.ok").wait_for(timeout=90000)
    point(page, page.locator("#c-status"), 1.6)
    print("   ", page.locator("#c-status").inner_text(), flush=True)
    click(page, page.locator("#wz-next"))

    say(page, "Pick the collection to bring in", "Each collection becomes a table. Nested parts become dotted columns: address.city, contact.email.")
    page.locator("#d-tree .wz-schema").first.wait_for()
    box = page.locator(f'input[data-table="{args.collection}"]')
    if box.count() == 0:
        click(page, page.locator('[data-toggle="(default)"]'))
        box.wait_for()
    point(page, page.locator(f'#d-tree .tbl-name[data-table="{args.collection}"]'), 0.6).click()
    page.locator("#d-cols", has_text="columns").wait_for()
    point(page, page.locator("#d-cols"), 2.2)
    others = page.locator(f'#d-tree input[type=checkbox]:checked:not([data-table="{args.collection}"])')
    check = page.locator(f'input[data-table="{args.collection}"]')
    if not check.is_checked():
        click(page, check)
    while others.count():
        others.first.uncheck()
    click(page, page.locator("#wz-next"))
    point(page, page.locator("#a-profile"), 0.5)
    page.uncheck("#a-studio")
    click(page, page.locator("#wz-next"))
    say(page, "Review and finish", "The documents are read, counted against Firebase's own count, stored as a snapshot and profiled. A mismatch would reject the import.")
    point(page, page.locator("#wz-body"), 1.6)
    click(page, page.locator("#wz-next"))
    page.locator("#wizard[open]").wait_for(state="detached", timeout=180000)
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for(timeout=60000)

    say(page, "The profile: what is really in these documents", "Empty values, distinct values, lengths and the most common values for every column, including the nested ones.")
    f = frame(page)
    f.locator("#summary .stat").first.wait_for(timeout=60000)
    point(page, f.locator("#summary"), 1.8)
    f.locator("#columns table").wait_for()
    point(page, f.locator("#columns"), 2.0)


def part_target(page: Page) -> None:
    say(page, "Add the destination: SQL Server", "New Target. The delivery will create a new table here and fill it with the validated customers.")
    click(page, page.get_by_role("button", name="New Target"))
    page.locator("#wizard[open]").wait_for()
    page.select_option("#g-type", "db")
    type_into(page, page.locator("#g-name"), TARGET)
    point(page, page.locator("#g-dialect"), 0.8)
    page.select_option("#g-dialect", "mssql")
    pause(0.8)
    click(page, page.locator("#wz-next"))

    say(page, "Connect to the SQL Server database", "A login that may create tables in this one scratch database and nowhere else.")
    type_into(page, page.locator("#c-host"), args.sql_host, 35)
    type_into(page, page.locator("#c-port"), args.sql_port, 35)
    type_into(page, page.locator("#c-database"), args.sql_db, 35)
    type_into(page, page.locator("#c-user"), args.sql_user, 35)
    type_into(page, page.locator("#c-password"), args.sql_password, 35)
    click(page, page.locator("#c-test"))
    page.locator("#c-status.ok").wait_for(timeout=60000)
    point(page, page.locator("#c-status"), 1.6)
    print("   ", page.locator("#c-status").inner_text(), flush=True)
    click(page, page.locator("#wz-next"))

    say(page, "Prove the account can write", "The wizard creates a scratch table, fills it, reads it and drops it again. Delivery is refused if this fails.")
    page.locator("#t-schema option", has_text="dbo").wait_for(state="attached")
    page.select_option("#t-schema", "dbo")
    pause(0.6)
    click(page, page.locator("#t-test"))
    page.locator("#t-status.ok").wait_for(timeout=90000)
    point(page, page.locator("#t-status"), 1.6)
    print("   ", page.locator("#t-status").inner_text(), flush=True)
    click(page, page.locator("#wz-next"))
    click(page, page.locator("#wz-next"))
    point(page, page.locator("#wz-body"), 1.6)
    click(page, page.locator("#wz-next"))
    page.locator("#wizard[open]").wait_for(state="detached", timeout=60000)
    pause(1.0)


def open_studio(page: Page):
    node = page.locator(f'#tree li[data-id="sys:{SYSTEM}"] li[data-id^="t"] > .node').first
    if not node.is_visible():
        click(page, page.locator(f'#tree li[data-id="sys:{SYSTEM}"] > .node .twist').first, 0.4)
    click(page, node)
    click(page, page.get_by_role("button", name="Mapping", exact=True))
    page.locator("#tabstrip .tab", has_text="Mapping").wait_for()
    f = frame(page)
    f.locator("#mapping-select").wait_for()
    return f


def part_mapping(page: Page) -> None:
    say(page, "Open the Mapping Studio", "The columns of the Firebase collection on one side, the SAP business-partner fields on the other. Built by hand here; with a Gemini key the AI proposes it.")
    f = open_studio(page)
    pause(1.0)
    click(page, f.locator("#new-set"))
    f.locator("#mapping-status", has_text="DRAFT").wait_for(timeout=30000)
    pause(0.8)

    say(page, "Point this mapping at SQL Server", "The destination is chosen with the mapping. The delivery panel will already have it selected.")
    point(page, f.locator("#target-select"), 0.8)
    f.locator("#target-select").select_option(label=f.locator("#target-select option", has_text=TARGET).first.inner_text())
    f.locator("#toast.show", has_text="Destination").wait_for(timeout=30000)
    point(page, f.locator("#target-select"), 1.4)

    say(page, "Map the columns to SAP fields", "The rules read the nested fields by their dotted names, for example address.city for the SAP city field.")
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

    say(page, "Check the rules on a sample of the real documents", "A dry run before anybody is asked to approve: how many sampled customers pass the SAP field checks, and what the destination says.")
    click(page, f.locator("#preflight"))
    f.locator("#preflight-panel").wait_for(timeout=120000)
    pause(1.5)
    f.locator("#preflight-panel .destination").wait_for(timeout=30000)
    point(page, f.locator("#preflight-panel"), 3.0)
    print("   check:", f.locator("#preflight-panel").inner_text().replace(chr(10), " | ")[:240], flush=True)


def part_run(page: Page) -> None:
    f = frame(page)
    say(page, "Submit the mapping for approval", "demo.author built it and submits it. Nothing runs until a second person approves.")
    click(page, f.locator("#submit-mapping"))
    f.locator("#mapping-status", has_text="SUBMITTED").wait_for(timeout=30000)
    pause(1.2)
    say(page, "A different person approves: now acting as demo.steward", "The approval records who and when. The author of a mapping can never approve it.")
    kit.act_as(page, "demo.steward")
    click(page, f.locator("#approve-mapping"))
    f.locator("#mapping-status", has_text="APPROVED").wait_for(timeout=30000)
    pause(1.2)
    say(page, "Run the pipeline", "Transform with the approved rules, find duplicate customers (the nested e-mail address and the tax number give them away), check every field.")
    click(page, f.locator("#run-pipeline"))
    page.locator("#tabstrip .tab", has_text="Governance").wait_for(timeout=300000)
    f = frame(page)
    f.locator("#gate").wait_for(timeout=60000)
    pause(1.5)

    say(page, "The governance gate", "It opens only when no critical defect is left. The sign-off is a separate act by a steward.")
    point(page, f.locator("#gate"), 2.5)
    print("   gate:", f.locator("#gate").inner_text().replace(chr(10), " | ")[:220], flush=True)
    click(page, f.locator("#sign-off"))
    f.locator("#toast.show", has_text="signed off by demo.steward").wait_for(timeout=30000)
    pause(1.6)

    say(page, "Deliver the validated customers into SQL Server", "The target is the one the mapping was pointed at. 'Show table & SQL' shows exactly what will be created, before anything is.")
    point(page, f.locator("#db-delivery"), 0.8)
    f.locator("#db-conn option", has_text=TARGET).wait_for(state="attached", timeout=30000)
    expect(f.locator("#db-conn")).to_have_value(TARGET, timeout=30000)
    point(page, f.locator("#db-conn"), 1.0)
    type_into(page, f.locator("#db-table"), args.table)
    click(page, f.locator("#db-preview"))
    f.locator("#db-ddl:not(.hidden)").wait_for(timeout=60000)
    point(page, f.locator("#db-ddl"), 3.0)
    print("   preview:", f.locator("#db-ddl").inner_text().splitlines()[0][:160], flush=True)
    say(page, "Deliver", "The table is created and filled in one all-or-nothing step: the rows are counted in SQL Server and every value is compared with what was sent.")
    click(page, f.locator("#db-load"))
    f.locator("#db-history table").wait_for(timeout=600000)
    pause(1.0)
    point(page, f.locator("#db-history"), 2.8)
    print("   delivery:", f.locator("#db-history tbody tr").first.inner_text().replace(chr(10), " | ").replace(chr(9), " ")[:260], flush=True)

    say(page, "What is really in SQL Server now", "Read straight from the database, not through this page: the new table, its row count and the first rows.")
    connector = sql_connector()
    total = connector.count("dbo", args.table)
    cols, rows = connector.preview("dbo", args.table, 3)
    names = [c.name for c in cols]
    wanted = ["LEGACY_KEY", "BUT000_NAME_ORG1", "ADRC_CITY1", "ADRC_COUNTRY", "ADR6_SMTP_ADDR", "KNB1_ZTERM", "MIG_RUN_ID"]
    lines = []
    for r in rows:
        lines.append("row " + r[names.index("LEGACY_KEY")])
        lines += [f"    {k}: {r[names.index(k)]!r}" for k in wanted[1:] if k in names]
    kit.banner(page, f"SQL Server {args.sql_db}  >  dbo.{args.table}  >  {total:,} rows", lines)
    print(f"   sql server holds {total} rows in dbo.{args.table}", flush=True)
    pause(5.0)
    kit.clear_banner(page)

    say(page, "Verify against the live table", "Every value in SQL Server is read again and compared with the validated records. An edit made in the database afterwards would show up here.")
    click(page, f.locator(".db-verify").first)
    f.locator("#toast.show", has_text="identical").wait_for(timeout=300000)
    point(page, f.locator("#toast"), 2.0)
    print("   verify:", f.locator("#toast").inner_text()[:160], flush=True)
    f.locator("#db-history .badge", has_text="still identical").first.wait_for(timeout=30000)
    point(page, f.locator("#db-history"), 1.6)

    say(page, "The reconciliation certificate", "Every record accounted for, and the SQL Server delivery is a line on it: how many rows, and that the values were compared.")
    click(page, page.get_by_role("button", name="Certificate", exact=True))
    page.locator("#tabstrip .tab", has_text="Certificate").wait_for(timeout=30000)
    cf = frame(page)
    pause(3.0)
    point(page, cf.locator("body"), 2.5)
    page.keyboard.press("End")
    pause(2.0)

    if args.rollback:
        say(page, "Roll the delivery back", "Exactly this run's rows are deleted again, and the table too when nothing else is in it; the reason is kept in the record.")
        page.locator("#tabstrip .tab", has_text="Governance").click()
        f = frame(page)
        click(page, f.locator(".db-rollback").first)
        f.locator("#db-history .badge", has_text="rolled back").first.wait_for(timeout=300000)
        point(page, f.locator("#db-history"), 2.0)
    say(page, "A Firebase collection, migrated into SQL Server", "Nested documents read, duplicates merged through their nested e-mail and tax number, gate and sign-off, rows counted and compared in SQL Server, certificate issued.")
    pause(4.0)


if __name__ == "__main__":
    run()
