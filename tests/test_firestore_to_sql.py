"""Firebase as the SOURCE, a SQL database as the TARGET: a Firestore collection with nested fields goes through the whole pipeline and is delivered into a real PostgreSQL table.

    docker compose -f docker-compose.servers.yml --profile firestore up -d --wait
    python -m pytest tests/test_firestore_to_sql.py

The documents carry nested maps (`address.city`, `contact.email`): Firestore's connector flattens them into dotted column names, so the mapping rules read `address.city` and the
duplicate detection has to recognise `contact.email` as an e-mail key.
"""

from __future__ import annotations

import os

import pytest

from src.services.lookup_service import PAYMENT_TERMS_SEED
from tests.test_db_targets import pg_query, pg_target, save_target, sign_off  # noqa: F401
from tests.test_module7_api import STEWARD, U, client, ctx, dataset, encoder, env  # noqa: F401
from tests.test_real_firestore import HOST, PORT, PROJECT, emulator_client, reachable

COLLECTION = f"fs2sql_customers_{os.getpid()}"
CITIES = [("Köln", "DE", "50667"), ("Wien", "AT", "1010"), ("Lyon", "FR", "69001"), ("Milano", "IT", "20121"), ("Zürich", "CH", "8001")]


@pytest.fixture()
def customers():
    """30 customers as Firestore documents, nested the way an app would store them; three of them are second copies of another customer."""
    if not reachable():
        pytest.skip("Firestore emulator is not running (docker compose -f docker-compose.servers.yml --profile firestore up -d --wait)")
    db = emulator_client()
    coll = db.collection(COLLECTION)
    for doc in coll.list_documents():
        doc.delete()
    for i in range(30):
        city, country, zip_ = CITIES[i % len(CITIES)]
        coll.document(f"C{i:03d}").set({
            "name": f"Customer Alpha {i:02d} GmbH", "tax_id": f"DE9{i:08d}", "terms": ["NET30", "NET60", "COD"][i % 3],
            "address": {"street": f"Hauptstrasse {i + 1}", "city": city, "zip": zip_, "country": country},
            "contact": {"email": f"accounts{i}@alpha{i}.example.com", "phone": f"+49 30 55{i:05d}"}})
    for n, original in enumerate((3, 7)):                                      # the same person under another name: only the e-mail address says so
        city, country, zip_ = CITIES[original % len(CITIES)]
        coll.document(f"D{n:03d}").set({
            "name": f"Zeta Wholesale {n} Ltd", "terms": "NET30", "address": {"street": f"Other Road {n + 90}", "city": "Bern", "zip": "3011", "country": "CH"},
            "contact": {"email": f"accounts{original}@alpha{original}.example.com"}})
    coll.document("D002").set({"name": "Eta Imports", "tax_id": "DE900000011", "terms": "COD",                    # and one that repeats a tax number
                               "address": {"street": "Third Lane 5", "city": "Graz", "zip": "8010", "country": "AT"}, "contact": {"email": "eta@eta.example.com"}})
    yield
    for doc in coll.list_documents():
        doc.delete()


def test_a_firebase_collection_is_migrated_into_a_sql_table(customers, client, ctx, pg_target):
    pg, conn = pg_target
    # ---- the source: a Firestore collection, read through the emulator
    saved = client.post("/api/connections", json={"name": "fb_source", "dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "emulator"})
    assert saved.status_code == 201, saved.text
    imp = client.post("/api/sources/import-db", json={"connection": "fb_source", "schema_name": "(default)", "table": COLLECTION, "table_name": "FB_CUSTOMERS"})
    assert imp.status_code == 201, imp.text
    reg = imp.json()
    assert reg["rows_landed"] == 33 and reg["control_status"] == "MATCH"
    columns = [c["name"] for c in client.get(f"/api/tables/{reg['table_id']}/snapshot").json()["columns"]]
    assert {"_id", "name", "address.city", "contact.email", "tax_id"} <= set(columns)                       # nested fields became dotted columns

    # ---- the mapping reads the dotted columns, and a SQL expression can too
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    mapping = client.post("/api/mapping-sets", json={"source_table_id": reg["table_id"]}).json()["mapping_set_id"]
    rules = [("BUT000", "NAME_ORG1", "DIRECT_COPY", "name", None, None), ("BUT000", "BP_EXT", "DIRECT_COPY", "_id", None, None),
             ("BUT000", "BU_SORT1", "SQL_EXPRESSION", "name", "upper(left(trim(\"name\"), 20))", None),
             ("ADRC", "STREET", "DIRECT_COPY", "address.street", None, None), ("ADRC", "CITY1", "DIRECT_COPY", "address.city", None, None),
             ("ADRC", "POST_CODE1", "DIRECT_COPY", "address.zip", None, None), ("ADRC", "COUNTRY", "VALUE_MAPPING", "address.country", None, {c: c for c in ("DE", "AT", "CH", "FR", "IT")}),
             ("ADR6", "SMTP_ADDR", "DIRECT_COPY", "contact.email", None, None), ("KNB1", "ZTERM", "VALUE_MAPPING", "terms", None, PAYMENT_TERMS_SEED),
             ("BUT000", "BU_GROUP", "STATIC_VALUE", None, "'BP01'", None), ("BUT000", "TYPE", "STATIC_VALUE", None, "'2'", None)]
    for table, column, rtype, source, logic, lookups in rules:
        r = client.post(f"/api/mapping/{mapping}/rules", json={"target_table": table, "target_column": column, "rule_type": rtype, "source_column": source,
                                                               "transformation_logic": logic, "lookups": lookups})
        assert r.status_code == 201, (column, r.text)
    pre = client.post(f"/api/mapping/{mapping}/preflight").json()
    assert pre["can_submit"], [(f["code"], f["message"][:160]) for f in pre["findings"]]
    assert client.post(f"/api/governance/submit-mapping/{mapping}").status_code == 200
    assert client.post(f"/api/governance/approve-mapping/{mapping}", headers=U(STEWARD)).status_code == 200

    # ---- the run: the nested e-mail and the tax number find the three copies
    run = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "FB_TO_SQL"})
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["readiness"]["state"] == "READY_FOR_LOAD", body["readiness"]
    assert body["dedup"]["duplicate_children"] == 3 and body["validation"]["valid_records"] == 30, body["dedup"]
    keys = {k["name"] for k in body["dedup"].get("exact_keys", [])} if isinstance(body["dedup"].get("exact_keys"), list) else set()
    run_id, valid = body["run_id"], body["validation"]["valid_records"]

    # ---- the delivery: a real PostgreSQL table
    assert save_target(client, pg).status_code == 201
    sign_off(client, run_id)
    done = client.post(f"/api/pipeline/load-db/{run_id}", json={"connection": "ora_like_target", "table_name": "MIG_FROM_FIRESTORE"})
    assert done.status_code == 201, done.text
    assert done.json()["rows_loaded"] == done.json()["rows_in_target"] == valid == 30
    rows = {r[0]: r[1:] for r in pg_query(conn, "SELECT legacy_key, but000_name_org1, adrc_city1, adrc_country, adr6_smtp_addr, knb1_zterm, but000_bu_sort1 FROM mig_target.mig_from_firestore")}
    assert len(rows) == 30 and "D000" not in rows and "D001" not in rows and "D002" not in rows                  # the copies were merged away, the originals delivered
    assert rows["C003"] == ("Customer Alpha 03 GmbH", "Milano", "IT", "accounts3@alpha3.example.com", "NT30", "CUSTOMER ALPHA 03 GM")
    assert rows["C004"][1:3] == ("Zürich", "CH")                                                                 # the umlaut arrived in the SQL table
    assert [c for c in client.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"] if c["name"].startswith("db_delivery_postgresql") and c["passed"]]
