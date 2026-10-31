"""The MongoDB connector against a REAL MongoDB server (Docker, see docker-compose.servers.yml), plus the flattening rules on their own.

    docker compose -f docker-compose.servers.yml --profile mongo up -d --wait
    python -m pytest tests/test_real_mongo.py

The server tests are skipped when it is not reachable. The fixtures are written natively with pymongo (root login) and read back through
the application's read-only login, so the tests check what the connector really reads: nested documents, arrays, missing fields and
nulls, numbers stored as text and as numbers, Decimal128, dates, ObjectIds, unicode, a field that only the last document has, and refusals.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import socket
from decimal import Decimal

import pytest

from src.connectors.base import ConnectionSpec, ConnectorAuthError, ConnectorError
from src.connectors.dialects import connector_for
from src.connectors.documents import MAX_COLUMNS, flatten
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

HOST, PORT, DB = "localhost", 27018, "legacy_docs"
RO = ("legacy_ro", "Mongo_ro_Test1")
ROOT = ("root", "Mongo_root_Test1")


def reachable() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ the rules, without a server
def test_flatten_nested_documents_arrays_nulls_and_types():
    from bson.decimal128 import Decimal128
    from bson.objectid import ObjectId
    oid = ObjectId("65f0c0ffee00000000000001")
    row = flatten({"_id": oid, "name": "Zoë \"Q\"", "address": {"city": "Köln", "geo": {"lat": 50.9, "lon": 6.9}}, "tags": ["a", {"b": 1}], "none": None, "empty": {}, "flag": False,
                   "amount": Decimal128("1234567.80"), "n": 7, "when": dt.datetime(2024, 2, 29, 12, 0, 0, tzinfo=dt.timezone.utc), "raw": b"\x00\xff"})
    assert row == {"_id": "65f0c0ffee00000000000001", "name": 'Zoë "Q"', "address.city": "Köln", "address.geo.lat": "50.9", "address.geo.lon": "6.9", "tags": '["a",{"b":1}]',
                   "none": "", "empty": "{}", "flag": "false", "amount": "1234567.80", "n": "7", "when": "2024-02-29 12:00:00", "raw": "00ff"}


def test_flatten_stops_at_the_depth_limit_and_refuses_colliding_names():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert flatten(deep) == {"a.b.c.d.e": '{"f":1}'}                                                      # 4 levels flattened, the rest kept whole
    with pytest.raises(ConnectorError, match="both become the column 'a.b'"):
        flatten({"a.b": 1, "a": {"b": 2}})


# ------------------------------------------------------------------ against the server
@pytest.fixture(scope="module")
def mongo():
    if not reachable():
        pytest.skip("MongoDB test server is not running (docker compose -f docker-compose.servers.yml --profile mongo up -d --wait)")
    from bson.decimal128 import Decimal128
    from pymongo import MongoClient
    root = MongoClient(HOST, PORT, username=ROOT[0], password=ROOT[1], authSource="admin", serverSelectionTimeoutMS=5000)
    db = root[DB]
    for name in db.list_collection_names():
        db.drop_collection(name)
    utc = dt.timezone.utc
    first = [
        {"cust_id": "00123", "name": 'Müller & Söhne, "Ltd"', "address": {"street": "Hauptstr. 1", "city": "Köln", "geo": {"lat": 50.94, "lon": 6.96}},
         "contacts": [{"name": "Ann", "phone": "+49 1"}, {"name": "Bob", "phone": None}], "tags": ["vip", "de"], "balance": Decimal128("1234567.89"),
         "created": dt.datetime(2024, 1, 5, 8, 30, tzinfo=utc), "active": True, "notes": "line one\nline two"},
        {"cust_id": 124, "name": "Café Ñandú", "balance": 7, "created": dt.datetime(2023, 12, 31, 23, 59, 59, tzinfo=utc), "active": False, "notes": None},
        {"cust_id": "00125", "name": "Zoë 😀 AG", "address": {"city": "Wien"}, "tags": [], "balance": 0.1},
    ]
    bulk = [{"cust_id": f"C{i:05d}", "name": f"Customer {i}", "address": {"city": f"City {i % 50}"}, "balance": i} for i in range(3000)]
    last = {"cust_id": "LAST", "name": "Late", "late_field": "only the last document has me"}
    db.customers.insert_many([*first, *bulk, last])
    db.collide.insert_many([{"a.b": 1, "a": {"b": 2}}])
    db.wide.insert_one({f"f{i}": i for i in range(MAX_COLUMNS + 5)})
    db.create_collection("empty")
    db.view_of_customers.drop()
    db.command("create", "customers_de", viewOn="customers", pipeline=[{"$match": {"address.city": "Köln"}}])
    yield
    root.drop_database(DB)
    root.close()


def spec(role=RO, **options) -> ConnectionSpec:
    return ConnectionSpec("mongodb", HOST, PORT, DB, role[0], role[1], {"connect_timeout": 10, **options})


def read_csv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def test_browse_test_columns_and_preview(mongo):
    c = connector_for(spec())
    assert c.test()["server_version"].startswith("MongoDB 7")
    assert DB in c.schemas() and "admin" not in c.schemas()
    tables = {t.name: t for t in c.tables(DB)}
    assert tables["customers"].kind == "TABLE" and tables["customers"].row_estimate == 3004 and tables["customers_de"].kind == "VIEW" and "system.views" not in tables
    cols = {m.name: m for m in c.columns(DB, "customers")}
    assert list(cols)[0] == "_id" and cols["_id"].is_primary_key and "address.geo.lat" in cols and "contacts" in cols
    assert cols["cust_id"].data_type == "int|str"                                                          # stored both ways: the catalog says so
    assert "late_field" not in cols                                                                        # outside the sample, found by the extract
    _, rows = c.preview(DB, "customers", 2)
    assert len(rows) == 2 and rows[0][list(cols).index("name")] == 'Müller & Söhne, "Ltd"'
    assert c.count(DB, "customers") == 3004


def test_extract_reads_every_document_and_every_field_exactly(mongo, tmp_path):
    c = connector_for(spec())
    out = c.extract_csv(DB, "customers", None, tmp_path / "customers.csv.gz", 50 * 1048576)
    assert out.rows == out.source_count == 3004
    header, rows = read_csv(out.path)
    assert header[0] == "_id" and "late_field" in header and header.count("late_field") == 1 and len(set(header)) == len(header)
    by = {r[header.index("cust_id")]: dict(zip(header, r)) for r in rows}
    a, b, z, late = by["00123"], by["124"], by["00125"], by["LAST"]
    assert a["name"] == 'Müller & Söhne, "Ltd"' and a["address.city"] == "Köln" and a["address.geo.lat"] == "50.94" and a["balance"] == "1234567.89"      # leading zero kept, exact decimal
    assert a["contacts"] == '[{"name":"Ann","phone":"+49 1"},{"name":"Bob","phone":null}]' and a["tags"] == '["vip","de"]' and a["active"] == "true"
    assert a["created"] == "2024-01-05 08:30:00" and a["notes"] == "line one\nline two" and len(a["_id"]) == 24
    assert b["name"] == "Café Ñandú" and b["balance"] == "7" and b["address.city"] == "" and b["notes"] == "" and b["active"] == "false"             # missing and null are both blank
    assert z["name"] == "Zoë 😀 AG" and z["tags"] == "[]" and z["balance"] == "0.1"
    assert late["late_field"] == "only the last document has me" and by["C00007"]["address.city"] == "City 7"
    sub = c.extract_csv(DB, "customers", ["_id", "name"], tmp_path / "two.csv.gz", 50 * 1048576)
    assert read_csv(sub.path)[0] == ["_id", "name"] and sub.rows == 3004


def test_the_application_can_only_read(mongo):
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure
    client = MongoClient(HOST, PORT, username=RO[0], password=RO[1], authSource=DB, serverSelectionTimeoutMS=5000)
    with pytest.raises(OperationFailure):
        client[DB].customers.insert_one({"x": 1})
    with pytest.raises(OperationFailure):
        client[DB].customers.delete_many({})
    assert not hasattr(connector_for(spec()), "load_table") and not hasattr(connector_for(spec()), "merge_table")           # the connector has no write path at all
    assert connector_for(spec()).count(DB, "customers") == 3004


def test_refusals_are_clear_and_leave_nothing_behind(mongo, tmp_path):
    c = connector_for(spec())
    with pytest.raises(ConnectorError, match="both become the column 'a.b'"):
        c.extract_csv(DB, "collide", None, tmp_path / "collide.csv.gz", 1048576)
    assert not (tmp_path / "collide.csv.gz").exists()
    with pytest.raises(ConnectorError, match="more than 500"):
        c.extract_csv(DB, "wide", None, tmp_path / "wide.csv.gz", 1048576)
    with pytest.raises(ConnectorError, match="not found"):
        c.extract_csv(DB, "nope", None, tmp_path / "x.csv.gz", 1048576)
    with pytest.raises(ConnectorError, match="not found in legacy_docs.customers"):
        c.extract_csv(DB, "customers", ["_id", "no_such_field"], tmp_path / "y.csv.gz", 50 * 1048576)
    with pytest.raises(ConnectorError, match="no documents"):
        c.columns(DB, "empty")
    with pytest.raises(ConnectorError, match="MB limit"):
        c.extract_csv(DB, "customers", None, tmp_path / "big.csv.gz", 1)   # an impossibly small cap
    assert not (tmp_path / "big.csv.gz").exists()
    with pytest.raises(ConnectorAuthError) as bad:
        connector_for(ConnectionSpec("mongodb", HOST, PORT, DB, "legacy_ro", "wrong-password", {"connect_timeout": 10})).test()
    assert "wrong-password" not in str(bad.value)
    with pytest.raises(ConnectorError, match="could not reach"):
        connector_for(ConnectionSpec("mongodb", HOST, 1, DB, "u", "p", {"connect_timeout": 1})).test()


# ------------------------------------------------------------------ through the application
def test_import_through_the_api_lands_a_source_with_control_total_and_a_document_key(mongo, client, ctx):
    saved = client.post("/api/connections", json={"name": "docs", "dialect": "mongodb", "host": HOST, "port": PORT, "database": DB, "user": RO[0], "password": RO[1],
                                                   "options": {"auth_source": DB}})
    assert saved.status_code == 201, saved.text
    assert RO[1] not in saved.text and saved.json()["password_source"] == "memory"
    assert client.post("/api/connections", json={"name": "docs_t", "dialect": "mongodb", "host": HOST, "port": PORT, "database": DB, "user": RO[0], "role": "target"}).status_code == 422
    assert client.post("/api/connections/test", json={"name": "docs"}).json()["ok"] is True
    assert client.get("/api/connections/docs/schemas").json() == [DB]
    r = client.post("/api/sources/import-db", json={"connection": "docs", "schema_name": DB, "table": "customers", "business_name": "Customers (documents)"})
    assert r.status_code == 201, r.text
    reg = r.json()
    assert reg["rows_landed"] == 3004 and reg["control_status"] == "MATCH" and reg["origin"]["kind"] == "database" and reg["origin"]["dialect"] == "mongodb"
    snap = client.get(f"/api/tables/{reg['table_id']}/snapshot").json()
    names = [c["name"] for c in snap["columns"]]
    assert "address.geo.lat" in names and "late_field" in names
    profile = client.post(f"/api/profile/{reg['table_id']}")
    assert profile.status_code == 200, profile.text                                                        # dotted column names go through profiling
    again = client.post("/api/sources/import-db", json={"connection": "docs", "schema_name": DB, "table": "collide"})
    assert again.status_code == 502 and "both become the column" in again.text
