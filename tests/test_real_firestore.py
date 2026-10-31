"""The Firestore (Google Firebase) connector: the rules without any server, against the official Firestore EMULATOR (Docker), and - only when you give it a key - against a
REAL Firebase project.

    docker compose -f docker-compose.servers.yml --profile firestore up -d --wait
    python -m pytest tests/test_real_firestore.py

    # against a real project (reads only; writes a few documents into a collection it creates and removes again, so use a scratch project):
    $env:FIREBASE_TEST_KEY_FILE = "C:\\path\\to\\service-account.json"      # a key with the Cloud Datastore User role
    $env:FIREBASE_TEST_PROJECT = "my-scratch-project"
    python -m pytest tests/test_real_firestore.py -k real_project

The emulator tests are skipped when it is not reachable. The fixtures are written natively with the Firestore client and read back through the application's connector, so
the tests check what the connector really reads: nested maps, arrays, missing fields and nulls, numbers stored as text and as numbers, timestamps, GeoPoints, document
references, bytes, unicode, a field that only the last document has, and refusals.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import os
import socket
from pathlib import Path

import pytest

from src.connectors.base import ConnectionSpec, ConnectorAuthError, ConnectorError
from src.connectors.dialects import connector_for
from src.connectors.documents import MAX_COLUMNS
from src.connectors.firestore import FirestoreConnector, flatten_document, parse_key
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

HOST, PORT, PROJECT = "localhost", 18080, "demo-erp"


def reachable() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5):
            return True
    except OSError:
        return False


class Snap:
    """What a Firestore document snapshot offers the connector."""
    def __init__(self, id, data):
        self.id, self._data = id, data

    def to_dict(self):
        return self._data


# ------------------------------------------------------------------ the rules, without a server
def test_flatten_document_types():
    from google.cloud.firestore_v1 import GeoPoint
    utc = dt.timezone.utc

    class DocumentReference:                       # the connector looks at the type name: the real class needs a client
        path = "customers/00123"

    row = flatten_document(Snap("00123", {"name": 'Zoë "Q"', "address": {"city": "Köln", "geo": GeoPoint(50.94, 6.96)}, "tags": ["a", {"b": 1}], "none": None, "empty": {},
                                          "flag": False, "n": 7, "ratio": 0.1, "when": dt.datetime(2024, 2, 29, 12, 0, 0, tzinfo=utc), "raw": b"\x00\xff", "owner": DocumentReference()}))
    assert row == {"_id": "00123", "name": 'Zoë "Q"', "address.city": "Köln", "address.geo.latitude": "50.94", "address.geo.longitude": "6.96", "tags": '["a",{"b":1}]', "none": "",
                   "empty": "{}", "flag": "false", "n": "7", "ratio": "0.1", "when": "2024-02-29 12:00:00", "raw": "00ff", "owner": "customers/00123"}
    assert list(row)[0] == "_id"                                                                          # the document ID leads, and is text: "00123" keeps its zeros


def test_a_field_called_id_underscore_is_refused_because_that_column_holds_the_document_id():
    with pytest.raises(ConnectorError, match="field called '_id'"):
        flatten_document(Snap("x", {"_id": "mine"}))


def test_the_service_account_key_is_checked_without_repeating_it():
    with pytest.raises(ConnectorError, match="not valid JSON") as e1:
        parse_key("-----BEGIN PRIVATE KEY----- SECRET")
    assert "SECRET" not in str(e1.value)
    with pytest.raises(ConnectorError, match="not a service-account key"):
        parse_key(json.dumps({"type": "authorized_user", "client_secret": "SECRET"}))
    with pytest.raises(ConnectorError, match="incomplete.*private_key"):
        parse_key(json.dumps({"type": "service_account", "project_id": "p", "client_email": "a@b", "token_uri": "https://x"}))
    ok = {"type": "service_account", "project_id": "p", "private_key": "k", "client_email": "a@b", "token_uri": "https://x"}
    assert parse_key(json.dumps(ok)) == ok


def test_a_bad_key_never_reaches_google_and_says_what_is_wrong():
    spec = ConnectionSpec("firestore", "firestore.googleapis.com", 443, "my-project", "sa", "not json at all", {})
    with pytest.raises(ConnectorError, match="not valid JSON") as e:
        connector_for(spec).test()
    assert "not json at all" not in str(e.value)


def test_connection_validation_for_firestore():
    from src.connectors.store import ConnectionInputError, ConnectionStore
    store = ConnectionStore(Path("."), allowed_hosts=("only-this-host",))
    good = store.validate({"name": "fb", "dialect": "firestore", "host": "", "database": "my-app-1234", "user": ""})
    assert good["host"] == "firestore.googleapis.com" and good["port"] == 443 and good["user"] == "service-account" and good["role"] == "source"    # the real service needs no allow-list entry
    assert ConnectionStore(Path(".")).validate({"name": "fb", "dialect": "firestore", "host": "localhost", "port": 18080, "database": "demo-erp", "user": "u", "options": {"database_id": "(default)"}})
    target = store.validate({"name": "fb", "dialect": "firestore", "host": "", "database": "my-app-1234", "user": "", "role": "target", "default_schema": "(default)"})
    assert target["role"] == "target" and target["default_schema"] == "(default)"
    for bad, message in (({"database": ""}, "project ID is required"), ({"database": "ab"}, "project ID is required"),
                         ({"host": "evil.example.com"}, "not on this server's allowed list|host must be"), ({"options": {"database_id": "Bad Name"}}, "database_id must be")):
        with pytest.raises(ConnectionInputError, match=message):
            store.validate({"name": "fb", "dialect": "firestore", "host": "firestore.googleapis.com", "database": "my-app-1234", "user": "u", **bad})


def test_a_google_error_is_translated_not_dumped():
    from google.api_core import exceptions as gx
    spec = ConnectionSpec("firestore", "firestore.googleapis.com", 443, "my-project", "sa", "secret-key-text", {})

    def boom(exc):
        def connect():
            raise exc
        return FirestoreConnector(spec, connect)
    with pytest.raises(ConnectorAuthError, match="Cloud Datastore Viewer"):
        boom(gx.PermissionDenied("403 secret-key-text")).test()
    with pytest.raises(ConnectorAuthError, match="rejected the service-account key"):
        boom(gx.Unauthenticated("401")).test()
    with pytest.raises(ConnectorError, match="was not found"):
        boom(gx.NotFound("404")).test()
    with pytest.raises(ConnectorError, match="could not reach Firestore") as e:
        boom(gx.DeadlineExceeded("timeout")).test()
    assert "secret-key-text" not in str(e.value)
    with pytest.raises(ConnectorError) as e2:
        boom(RuntimeError("oops secret-key-text")).test()
    assert "secret-key-text" not in str(e2.value)


# ------------------------------------------------------------------ against the emulator
@pytest.fixture(scope="module")
def emulator():
    if not reachable():
        pytest.skip("Firestore emulator is not running (docker compose -f docker-compose.servers.yml --profile firestore up -d --wait)")
    yield from seeded(emulator_client(), "legacy")


def emulator_client():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    os.environ["FIRESTORE_EMULATOR_HOST"] = f"{HOST}:{PORT}"
    try:
        return firestore.Client(project=PROJECT, credentials=AnonymousCredentials())
    finally:
        os.environ.pop("FIRESTORE_EMULATOR_HOST", None)


def seeded(db, prefix: str):
    """The fixtures, written natively; yields; then removes everything it wrote."""
    from google.cloud.firestore_v1 import GeoPoint
    utc = dt.timezone.utc
    names = [f"{prefix}_customers", f"{prefix}_collide", f"{prefix}_wide", f"{prefix}_empty"]
    for name in names:
        for doc in db.collection(name).list_documents():
            doc.delete()
    cust = db.collection(names[0])
    cust.document("00123").set({"cust_id": "00123", "name": 'Müller & Söhne, "Ltd"', "address": {"street": "Hauptstr. 1", "city": "Köln", "geo": GeoPoint(50.94, 6.96)},
                                "contacts": [{"name": "Ann", "phone": "+49 1"}, {"name": "Bob", "phone": None}], "tags": ["vip", "de"], "balance": 1234567.5,
                                "created": dt.datetime(2024, 1, 5, 8, 30, tzinfo=utc), "active": True, "notes": "line one\nline two", "raw": b"\x00\xff",
                                "owner": db.collection(names[0]).document("00124")})
    cust.document("00124").set({"cust_id": 124, "name": "Café Ñandú", "balance": 7, "created": dt.datetime(2023, 12, 31, 23, 59, 59, tzinfo=utc), "active": False, "notes": None})
    cust.document("00125").set({"cust_id": "00125", "name": "Zoë 😀 AG", "address": {"city": "Wien"}, "tags": [], "balance": 0.1})
    batch = db.batch()
    for i in range(3000):
        batch.set(cust.document(f"C{i:05d}"), {"cust_id": f"C{i:05d}", "name": f"Customer {i}", "address": {"city": f"City {i % 50}"}, "balance": i})
        if i % 400 == 399:
            batch.commit()
            batch = db.batch()
    batch.commit()
    cust.document("ZLAST").set({"cust_id": "LAST", "name": "Late", "late_field": "only the last document has me"})
    db.collection(names[1]).document("one").set({"a.b": 1, "a": {"b": 2}})
    db.collection(names[2]).document("one").set({f"f{i}": i for i in range(MAX_COLUMNS + 5)})
    yield names
    for name in names:
        for doc in db.collection(name).list_documents():
            doc.delete()


def spec(**options) -> ConnectionSpec:
    return ConnectionSpec("firestore", HOST, PORT, PROJECT, "emulator", "", {"connect_timeout": 10, **options})


def read_csv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def test_browse_test_columns_and_preview(emulator):
    c = connector_for(spec())
    assert c.test()["server_version"] == "Cloud Firestore (emulator)"
    assert c.schemas() == ["(default)"]
    tables = {t.name: t for t in c.tables("(default)")}
    assert tables["legacy_customers"].kind == "TABLE" and tables["legacy_customers"].row_estimate == 3004
    cols = {m.name: m for m in c.columns("(default)", "legacy_customers")}
    assert list(cols)[0] == "_id" and cols["_id"].is_primary_key and "address.geo.latitude" in cols and "contacts" in cols
    assert cols["cust_id"].data_type == "int|str"                                                          # stored both ways: the catalog says so
    assert "late_field" not in cols                                                                        # outside the sample (the first 1000 by ID), found by the extract
    _, rows = c.preview("(default)", "legacy_customers", 2)
    assert len(rows) == 2 and rows[0][list(cols).index("_id")] == "00123" and rows[0][list(cols).index("name")] == 'Müller & Söhne, "Ltd"'
    assert c.count("(default)", "legacy_customers") == 3004


def test_extract_reads_every_document_and_every_field_exactly(emulator, tmp_path):
    c = connector_for(spec())
    out = c.extract_csv("(default)", "legacy_customers", None, tmp_path / "customers.csv.gz", 50 * 1048576)
    assert out.rows == out.source_count == 3004                                                            # more than one page of 1000: the paging loses nothing
    header, rows = read_csv(out.path)
    assert header[0] == "_id" and "late_field" in header and header.count("late_field") == 1 and len(set(header)) == len(header)
    by = {r[0]: dict(zip(header, r)) for r in rows}
    assert len(by) == 3004
    a, b, z, late = by["00123"], by["00124"], by["00125"], by["ZLAST"]
    assert a["name"] == 'Müller & Söhne, "Ltd"' and a["address.city"] == "Köln" and a["address.geo.latitude"] == "50.94" and a["balance"] == "1234567.5"
    assert a["contacts"] == '[{"name":"Ann","phone":"+49 1"},{"name":"Bob","phone":null}]' and a["tags"] == '["vip","de"]' and a["active"] == "true"
    assert a["created"] == "2024-01-05 08:30:00" and a["notes"] == "line one\nline two" and a["raw"] == "00ff" and a["owner"].endswith("legacy_customers/00124")
    assert a["cust_id"] == "00123"                                                                         # the leading zeros of a text key survive
    assert b["name"] == "Café Ñandú" and b["balance"] == "7" and b["address.city"] == "" and b["notes"] == "" and b["active"] == "false"             # missing and null are both blank
    assert z["name"] == "Zoë 😀 AG" and z["tags"] == "[]" and z["balance"] == "0.1"
    assert late["late_field"] == "only the last document has me" and by["C00007"]["address.city"] == "City 7"
    sub = c.extract_csv("(default)", "legacy_customers", ["_id", "name"], tmp_path / "two.csv.gz", 50 * 1048576)
    assert read_csv(sub.path)[0] == ["_id", "name"] and sub.rows == 3004


def test_reading_is_unchanged_by_the_write_path(emulator):
    c = connector_for(spec())
    assert c.count("(default)", "legacy_customers") == 3004


def test_refusals_are_clear_and_leave_nothing_behind(emulator, tmp_path):
    c = connector_for(spec())
    with pytest.raises(ConnectorError, match="more than 500"):
        c.extract_csv("(default)", "legacy_wide", None, tmp_path / "wide.csv.gz", 1048576)
    with pytest.raises(ConnectorError, match="was not found"):
        c.extract_csv("(default)", "nope", None, tmp_path / "x.csv.gz", 1048576)
    with pytest.raises(ConnectorError, match="not found in \\(default\\).legacy_customers"):
        c.extract_csv("(default)", "legacy_customers", ["_id", "no_such_field"], tmp_path / "y.csv.gz", 50 * 1048576)
    with pytest.raises(ConnectorError, match="not 'other'"):
        c.count("other", "legacy_customers")
    with pytest.raises(ConnectorError, match="MB limit"):
        c.extract_csv("(default)", "legacy_customers", None, tmp_path / "big.csv.gz", 1)                   # an impossibly small cap
    assert not (tmp_path / "big.csv.gz").exists()
    with pytest.raises(ConnectorError, match="could not reach the Firestore emulator"):
        connector_for(ConnectionSpec("firestore", HOST, 1, PROJECT, "u", "", {"connect_timeout": 1})).test()


# ------------------------------------------------------------------ through the application
def test_import_through_the_api_lands_a_source_with_control_total_and_a_document_key(emulator, client, ctx):
    saved = client.post("/api/connections", json={"name": "fb", "dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "emulator"})
    assert saved.status_code == 201, saved.text
    assert client.get("/api/connections/fb/schemas").json() == ["(default)"]                                  # the emulator needs no key: a saved connection without one works
    as_target = client.post("/api/connections", json={"name": "fb_t", "dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "u", "role": "target", "default_schema": "(default)"})
    assert as_target.status_code == 201, as_target.text                                                       # Firestore can receive a delivery since the target path was added
    # no password at all: the emulator needs none. The saved connection has none either, so the application asks for it; the inline test is how a form checks it first
    inline = client.post("/api/connections/test", json={"dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "emulator", "password": ""})
    assert inline.status_code == 200 and inline.json()["ok"] is True, inline.text
    assert "Firestore" in inline.json()["server_version"]


def test_import_through_the_api_with_a_key_kept_in_memory(emulator, client, ctx):
    """The emulator ignores the key; the point is the path: a saved connection, the key in memory only, browse, import, profile."""
    saved = client.post("/api/connections", json={"name": "fb2", "dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "emulator", "password": "x" * 3000})
    assert saved.status_code == 201, saved.text                                                              # a 3 KB key is accepted
    assert "x" * 50 not in saved.text and saved.json()["password_source"] == "memory"
    assert client.get("/api/connections/fb2/schemas").json() == ["(default)"]
    r = client.post("/api/sources/import-db", json={"connection": "fb2", "schema_name": "(default)", "table": "legacy_customers", "business_name": "Customers (Firestore)"})
    assert r.status_code == 201, r.text
    reg = r.json()
    assert reg["rows_landed"] == 3004 and reg["control_status"] == "MATCH" and reg["origin"]["kind"] == "database" and reg["origin"]["dialect"] == "firestore"
    snap = client.get(f"/api/tables/{reg['table_id']}/snapshot").json()
    names = [c["name"] for c in snap["columns"]]
    assert "address.geo.latitude" in names and "late_field" in names and names[0] == "_id"
    assert client.post(f"/api/profile/{reg['table_id']}").status_code == 200                                # dotted column names go through profiling
    again = client.post("/api/sources/import-db", json={"connection": "fb2", "schema_name": "(default)", "table": "legacy_wide"})
    assert again.status_code == 502 and "more than 500" in again.text


# ------------------------------------------------------------------ a REAL Firebase project (only with a key)
@pytest.fixture(scope="module")
def real_project():
    key_file, project = os.environ.get("FIREBASE_TEST_KEY_FILE"), os.environ.get("FIREBASE_TEST_PROJECT")
    if not key_file or not project:
        pytest.skip("no real Firebase project given (set FIREBASE_TEST_KEY_FILE to a service-account key file and FIREBASE_TEST_PROJECT to its project ID)")
    from google.cloud import firestore
    from google.oauth2 import service_account
    text = Path(key_file).read_text(encoding="utf-8")
    creds = service_account.Credentials.from_service_account_info(parse_key(text), scopes=["https://www.googleapis.com/auth/datastore"])
    db = firestore.Client(project=project, credentials=creds)
    gen = seeded_small(db)
    names = next(gen)
    yield project, text, names
    for _ in gen:
        pass


def seeded_small(db):
    """Only a few documents in a collection of its own: a real project is billed per read and write."""
    utc = dt.timezone.utc
    name = f"erp_migration_test_{os.getpid()}"
    col = db.collection(name)
    col.document("00123").set({"cust_id": "00123", "name": "Müller & Söhne", "address": {"city": "Köln"}, "created": dt.datetime(2024, 1, 5, 8, 30, tzinfo=utc), "tags": ["a", "b"]})
    col.document("00124").set({"cust_id": 124, "name": "Café Ñandú", "notes": None})
    col.document("ZLAST").set({"cust_id": "LAST", "late_field": "only the last document has me"})
    yield [name]
    for doc in col.list_documents():
        doc.delete()


def test_real_project(real_project, tmp_path):
    project, key, (name,) = real_project
    c = connector_for(ConnectionSpec("firestore", "firestore.googleapis.com", 443, project, "key", key, {"connect_timeout": 30}))
    assert c.test()["server_version"] == "Cloud Firestore"
    assert name in {t.name for t in c.tables("(default)")}
    assert c.count("(default)", name) == 3
    out = c.extract_csv("(default)", name, None, tmp_path / "real.csv.gz", 1048576)
    header, rows = read_csv(out.path)
    by = {r[0]: dict(zip(header, r)) for r in rows}
    assert out.rows == out.source_count == 3 and by["00123"]["name"] == "Müller & Söhne" and by["00123"]["created"] == "2024-01-05 08:30:00" and by["00123"]["cust_id"] == "00123"
    assert by["ZLAST"]["late_field"] == "only the last document has me"
    with pytest.raises(ConnectorAuthError) as e:
        connector_for(ConnectionSpec("firestore", "firestore.googleapis.com", 443, project, "key", key.replace("PRIVATE KEY", "PRIVATE  KEY"), {"connect_timeout": 30})).test()
    assert "PRIVATE" not in str(e.value)



# ====================================================================== delivering INTO Firestore (a target)
from src.connectors.base import ColumnDef  # noqa: E402
from tests.test_db_targets import ready_run, sign_off  # noqa: E402,F401  (a signed-off run, ready for delivery)

COLS = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("BUT000_NAME_ORG1", "text", 40), ColumnDef("ADRC_CITY1", "text", 40)]
ROWS = [("00123", 7, "Müller & Söhne, \"Ltd\"", "Köln"), ("A/B", 7, "Café Ñandú", None), ("00125", 7, "Zoë 😀 AG", "Wien")]
BIG = 1200


@pytest.fixture
def target(emulator):
    """A write connector and the names of the collections a test used, removed afterwards."""
    used: list[str] = []
    db = emulator_client()

    def new_name(stem="mig_t"):
        name = f"{stem}_{os.getpid()}_{len(used)}"
        used.append(name)
        return name
    yield connector_for(spec()), new_name, db
    for name in used:
        for doc in db.collection(name).list_documents():
            doc.delete()


def test_a_delivery_writes_documents_reads_them_back_and_names_the_key(target):
    c, new, db = target
    name = new()
    out = c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(ROWS), expected_rows=3)
    assert out.rows_inserted == out.rows_in_target == 3 and out.values_verified and out.cells_compared == 12 and out.value_digest and not out.replaced_existing
    assert "new collection " + name in out.ddl and "Document ID = LEGACY_KEY" in out.ddl
    docs = {d.id: d.to_dict() for d in db.collection(name).stream()}
    assert set(docs) == {"00123", "A%2FB", "00125"}                                                          # a '/' cannot be in an ID; the leading zeros are kept
    assert docs["00123"] == {"LEGACY_KEY": "00123", "MIG_RUN_ID": 7, "BUT000_NAME_ORG1": 'Müller & Söhne, "Ltd"', "ADRC_CITY1": "Köln"}
    assert docs["A%2FB"]["LEGACY_KEY"] == "A/B" and docs["A%2FB"]["ADRC_CITY1"] is None and isinstance(docs["00125"]["MIG_RUN_ID"], int)
    assert docs["00125"]["BUT000_NAME_ORG1"] == "Zoë 😀 AG"
    held = c.fingerprint_table("(default)", name, [x.name for x in COLS], ["LEGACY_KEY"])
    assert held.digest == out.value_digest and held.rows == 3                                                  # what verify will compare, later


def test_a_delivery_only_goes_into_a_new_collection_and_says_how_else(target):
    c, new, db = target
    name = new()
    c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(ROWS))
    with pytest.raises(ConnectorError, match="already exists and holds documents"):
        c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter([("zzz", 8, "x", "y")]))
    with pytest.raises(ConnectorError, match="NEW collection.*roll the earlier delivery back"):
        c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(ROWS), mode="replace")
    assert len(list(db.collection(name).stream())) == 3                                                       # untouched by the refused attempts
    with pytest.raises(ConnectorError, match="delta \\(merge\\) deliveries into Firestore are not supported"):
        c.merge_table("(default)", name, COLS, "LEGACY_KEY", iter(ROWS))
    with pytest.raises(ConnectorError, match="cannot be a Firestore collection name"):
        c.load_table("(default)", "bad/name", COLS, ["LEGACY_KEY"], iter(ROWS))
    with pytest.raises(ConnectorError, match="this connection writes to the Firestore database '\\(default\\)'"):
        c.load_table("other", new(), COLS, ["LEGACY_KEY"], iter(ROWS))


def test_a_failed_delivery_leaves_nothing_behind(target):
    c, new, db = target
    name = new()
    many = [(f"K{i:05d}", 1, f"Name {i}", "City") for i in range(BIG)]
    with pytest.raises(ConnectorError, match="two records have the key 'K00007'"):                           # a duplicate key at the END: 1,200 documents were already written
        c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter([*many, ("K00007", 1, "dup", "x")]))
    assert not list(db.collection(name).list_documents())
    with pytest.raises(ConnectorError, match="row count check failed.*nothing was kept"):
        c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(many[:5]), expected_rows=6)
    assert not list(db.collection(name).list_documents())

    def explode():
        yield from many[:700]
        raise RuntimeError("the source died half way")
    with pytest.raises(RuntimeError, match="half way"):
        c.load_table("(default)", name, COLS, ["LEGACY_KEY"], explode())
    assert not list(db.collection(name).list_documents())                                                     # 700 written, 700 removed again
    ok = c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(many))                                    # and a good delivery still works afterwards, across several batches
    assert ok.rows_in_target == BIG


def test_verify_notices_a_document_changed_after_the_delivery(target):
    c, new, db = target
    name = new()
    out = c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(ROWS))
    names = [x.name for x in COLS]
    assert c.fingerprint_table("(default)", name, names, ["LEGACY_KEY"]).differing_columns(Fingerprint_of(ROWS)) == []
    db.collection(name).document("00125").update({"ADRC_CITY1": "Graz"})                                      # someone edits a document in the Firebase console
    held = c.fingerprint_table("(default)", name, names, ["LEGACY_KEY"])
    assert held.digest != out.value_digest and Fingerprint_of(ROWS).differing_columns(held) == ["ADRC_CITY1"]
    with pytest.raises(ConnectorError, match="was not found"):
        c.fingerprint_table("(default)", "no_such_collection_here", names, ["LEGACY_KEY"])


def Fingerprint_of(rows):
    from src.connectors.fingerprint import Fingerprint
    fp = Fingerprint([x.name for x in COLS], [0])
    for r in rows:
        fp.add(r)
    return fp


def test_rollback_removes_exactly_the_documents_of_the_run(target):
    c, new, db = target
    name = new()
    c.load_table("(default)", name, COLS, ["LEGACY_KEY"], iter(ROWS))
    db.collection(name).document("other").set({"LEGACY_KEY": "other", "MIG_RUN_ID": 99, "BUT000_NAME_ORG1": "another run's", "ADRC_CITY1": "x"})
    with pytest.raises(ConnectorError, match="holds 3 document\\(s\\) of run 7 but 5 were delivered"):
        c.rollback_delivery("(default)", name, "MIG_RUN_ID", 7, expected_rows=5)
    out = c.rollback_delivery("(default)", name, "MIG_RUN_ID", 7, expected_rows=3)
    assert out.removed == 3 and out.remaining == 1 and not out.table_dropped
    assert [d.id for d in db.collection(name).stream()] == ["other"]                                          # the other run's document is untouched
    with pytest.raises(ConnectorError, match="holds no documents of run 7"):
        c.rollback_delivery("(default)", name, "MIG_RUN_ID", 7)
    gone = c.rollback_delivery("(default)", name, "MIG_RUN_ID", 99)
    assert gone.removed == 1 and gone.remaining == 0 and gone.table_dropped                                    # nothing left: the collection is gone
    with pytest.raises(ConnectorError, match="was not found"):
        c.rollback_delivery("(default)", name, "MIG_RUN_ID", 99)


def test_the_write_probe_writes_reads_and_cleans_up(target):
    c, new, db = target
    before = {x.id for x in db.collections()}
    assert "succeeded in Firestore database (default)" in c.probe_write("(default)")
    assert {x.id for x in db.collections()} == before                                                         # the scratch collection is gone
    with pytest.raises(ConnectorError, match="writes to the Firestore database"):
        c.probe_write("elsewhere")


def test_document_ids_are_made_safe():
    from src.connectors.firestore import FirestoreConnector as F
    assert F.doc_id("a/b") == "a%2Fb" and F.doc_id("100%") == "100%25" and F.doc_id(123) == "123" and F.doc_id("00123") == "00123"
    assert F.doc_id("..") != ".." and F.doc_id("__x__") != "__x__"
    with pytest.raises(ConnectorError, match="empty key"):
        F.doc_id("  ")
    with pytest.raises(ConnectorError, match="1,500 bytes"):
        F.doc_id("x" * 1501)


def test_a_signed_off_run_is_delivered_verified_and_rolled_back_through_the_api(emulator, client, ctx, ready_run):
    """The whole path of the application: connection (role target) > write test > preview > gate > delivery > verify > certificate > rollback."""
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    saved = client.post("/api/connections", json={"name": "fb_target", "dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "emulator",
                                                  "role": "target", "default_schema": "(default)"})
    assert saved.status_code == 201, saved.text
    assert client.post("/api/connections/fb_target/test-write", json={"schema_name": "(default)"}).json()["ok"] is True
    asked = {"connection": "fb_target", "table_name": f"MIG_BP_{os.getpid()}"}
    pv = client.get(f"/api/pipeline/load-db/{run_id}/preview", params=asked).json()
    assert pv["rows"] == valid and "Document ID = LEGACY_KEY" in pv["ddl"]
    sign_off(client, run_id)
    done = client.post(f"/api/pipeline/load-db/{run_id}", json=asked)
    assert done.status_code == 201, done.text
    d = done.json()
    assert d["rows_loaded"] == d["rows_in_target"] == d["valid_records"] == valid and d["dialect"] == "firestore" and d["values_verified"]
    db = emulator_client()
    docs = list(db.collection(asked["table_name"]).stream())
    assert len(docs) == valid and all(x.to_dict()["MIG_RUN_ID"] == run_id for x in docs)
    assert client.post(f"/api/pipeline/load-db/{run_id}", json=asked).status_code == 422                       # not twice
    refused = client.post(f"/api/pipeline/load-db/{run_id}", json={**asked, "mode": "replace"})
    assert refused.status_code == 422 and "NEW collection" in refused.text
    ver = client.post(f"/api/pipeline/verify-db/{run_id}", json={"connection": "fb_target", "table_name": asked["table_name"]}).json()
    assert ver["verified"] is True and ver["rows_in_target"] == valid
    checks = {c["name"]: c for c in client.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}
    assert any(k.startswith("db_delivery_firestore_") and v["passed"] for k, v in checks.items())
    wording = next(v["detail"] for k, v in checks.items() if k.startswith("db_values_firestore_"))
    assert "after writing, before the delivery was accepted" in wording and "before the commit" not in wording     # Firestore has no commit: the certificate must not say it has
    docs[0].reference.update({"BUT000_NAME_ORG1": "TAMPERED"})                                                 # an edit in the Firebase console is caught by the next verify
    again = client.post(f"/api/pipeline/verify-db/{run_id}", json={"connection": "fb_target", "table_name": asked["table_name"]}).json()
    assert again["verified"] is False and "BUT000_NAME_ORG1" in again["differing_columns"]
    docs[0].reference.update({"BUT000_NAME_ORG1": docs[0].to_dict()["BUT000_NAME_ORG1"]})
    rb = client.post(f"/api/pipeline/rollback-db/{run_id}", json={"connection": "fb_target", "table_name": asked["table_name"], "reason": "rehearsal"})
    assert rb.status_code == 200, rb.text
    assert rb.json()["rows_removed"] == valid and rb.json()["table_dropped"] is True
    assert not list(db.collection(asked["table_name"]).list_documents())




def test_the_delivery_preview_says_what_firebase_will_do_before_anything_is_written(emulator, client, ctx, ready_run):
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    saved = client.post("/api/connections", json={"name": "fb_prev", "dialect": "firestore", "host": HOST, "port": PORT, "database": PROJECT, "user": "emulator", "role": "target",
                                                  "default_schema": "(default)"})
    assert saved.status_code == 201, saved.text
    table = f"MIG_PREV_{os.getpid()}"
    fresh = client.get(f"/api/pipeline/load-db/{run_id}/preview", params={"connection": "fb_prev", "table_name": table}).json()
    levels = {c["code"]: c["level"] for c in fresh["checks"]}
    assert levels == {"DEST_COST": "info"} and f"{valid:,} document(s)" in fresh["checks"][0]["message"] and "up to" not in fresh["checks"][0]["message"]     # exact: this run's records
    db = emulator_client()
    db.collection(table).document("someone-elses").set({"a": 1})
    try:
        taken = client.get(f"/api/pipeline/load-db/{run_id}/preview", params={"connection": "fb_prev", "table_name": table}).json()
        blocked = next(c for c in taken["checks"] if c["code"] == "DEST_COLLECTION_EXISTS")
        assert blocked["level"] == "block" and "roll the earlier delivery back" in blocked["next_step"]
    finally:
        for doc in db.collection(table).list_documents():
            doc.delete()
    bad = client.get(f"/api/pipeline/load-db/{run_id}/preview", params={"connection": "fb_prev", "table_name": "bad/name"})
    assert bad.status_code == 422 and "cannot be a Firestore collection name" in bad.text
