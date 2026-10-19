"""Database connectors: browsing, read-only extraction, control totals, secrets, and the import into the catalog.

PostgreSQL is tested against the real test server (a throw-away schema `legacy_ar` in the test database). Oracle, SQL Server and
MySQL are tested with a fake DB-API connection that answers the catalog queries: that proves the flow, quoting, limits and the
read-only statements, but those three have not been run against real servers of their own kind.
"""

from __future__ import annotations

import csv
import gzip
import json
import time
from decimal import Decimal
from pathlib import Path

import psycopg2
import pytest

from src.api.settings import Settings
from src.connectors.base import ConnectionSpec, ConnectorAuthError, ConnectorError, to_text
from src.connectors.dialects import CONNECTORS, MssqlConnector, MysqlConnector, OracleConnector, PostgresConnector, connector_for
from src.connectors.store import ConnectionInputError, ConnectionStore
from src.db.models import CatDataSource, CatTable
from tests.test_module7_api import client, ctx, dataset, encoder, env, make_client, make_ctx  # noqa: F401


# ------------------------------------------------------------------ a real PostgreSQL source
@pytest.fixture()
def pg(engine):
    url = engine.url
    params = dict(host=url.host or "localhost", port=url.port or 5432, database=url.database, user=url.username, password=url.password or "")
    conn = psycopg2.connect(host=params["host"], port=params["port"], dbname=params["database"], user=params["user"], password=params["password"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS legacy_ar CASCADE; CREATE SCHEMA legacy_ar;")
    cur.execute("""CREATE TABLE legacy_ar.customers (
        cust_id integer PRIMARY KEY, cust_name varchar(60) NOT NULL, zip varchar(10), amount numeric(12,2), created timestamp, active boolean, notes text);
        CREATE TABLE legacy_ar.order_lines (order_id integer, line_no integer, sku varchar(20), PRIMARY KEY (order_id, line_no));
        CREATE TABLE legacy_ar.no_key (a text, b text);
        CREATE VIEW legacy_ar.active_customers AS SELECT cust_id, cust_name FROM legacy_ar.customers WHERE active;
        CREATE TABLE legacy_ar."Odd Name" ("Col One" text, "select" text);""")
    cur.execute("""INSERT INTO legacy_ar.customers VALUES
        (1, 'Müller & Söhne, "Ltd"', '02134', 10.50, '2024-01-05 08:30:00', true, E'line one\\nline two'),
        (2, 'Café Ñandú', '10001', 7, NULL, false, NULL),
        (3, 'Zoë AG', NULL, 1234567.89, '2023-12-31 23:59:59', NULL, ''),
        (4, 'Acme', '80331', 0.10, '2024-02-29 12:00:00', true, 'plain')""")
    cur.execute("INSERT INTO legacy_ar.order_lines VALUES (1, 1, 'A-1'), (1, 2, 'B-2'), (2, 1, 'A-1')")
    cur.execute("INSERT INTO legacy_ar.no_key VALUES ('x', 'y'), ('x', 'y')")
    cur.execute("INSERT INTO legacy_ar.\"Odd Name\" VALUES ('v1', 'v2')")
    cur.execute("ANALYZE legacy_ar.customers")
    yield params
    cur.execute("DROP SCHEMA IF EXISTS legacy_ar CASCADE")
    conn.close()


def spec_of(p, **override):
    return ConnectionSpec(dialect="postgresql", host=p["host"], port=p["port"], database=p["database"], user=p["user"], password=override.get("password", p["password"]))


def test_connect_browse_and_describe_a_real_postgres(pg):
    c = PostgresConnector(spec_of(pg))
    info = c.test()
    assert info["ok"] and info["server_version"] and info["latency_ms"] >= 0
    assert "legacy_ar" in c.schemas() and "pg_catalog" not in c.schemas() and "pg_catalog" in c.schemas(include_system=True)

    tables = {t.name: t for t in c.tables("legacy_ar")}
    assert set(tables) == {"customers", "order_lines", "no_key", "active_customers", "Odd Name"}
    assert tables["active_customers"].kind == "VIEW" and tables["customers"].kind == "TABLE"
    assert [t.name for t in c.tables("legacy_ar", include_views=False, name_filter="CUST")] == ["customers"]

    cols = {col.name: col for col in c.columns("legacy_ar", "customers")}
    assert [col.name for col in c.columns("legacy_ar", "customers")] == ["cust_id", "cust_name", "zip", "amount", "created", "active", "notes"]
    assert cols["cust_id"].is_primary_key and not cols["cust_name"].is_primary_key and not cols["cust_name"].nullable and cols["zip"].nullable
    assert cols["cust_name"].data_type == "character varying(60)" and cols["cust_name"].length == 60 and cols["amount"].data_type == "numeric(12,2)"
    assert {col.name for col in c.columns("legacy_ar", "order_lines") if col.is_primary_key} == {"order_id", "line_no"}   # composite key
    assert not any(col.is_primary_key for col in c.columns("legacy_ar", "no_key"))
    with pytest.raises(ConnectorError, match="was not found"):
        c.columns("legacy_ar", "nope")


def test_connection_failures_are_clear_and_never_echo_the_password(pg):
    with pytest.raises(ConnectorAuthError) as bad:
        PostgresConnector(spec_of(pg, password="definitely-wrong-pw")).test()
    assert "definitely-wrong-pw" not in str(bad.value) and "rejected" in str(bad.value)
    started = time.monotonic()
    with pytest.raises(ConnectorError, match="cannot connect"):
        PostgresConnector(ConnectionSpec("postgresql", "127.0.0.1", 1, "x", "u", "secret-pw", {"connect_timeout": 2})).test()
    assert time.monotonic() - started < 8


def test_extract_is_exact_text_counted_in_the_same_transaction_and_streams_to_gzip(pg, tmp_path):
    c = PostgresConnector(spec_of(pg))
    result = c.extract_csv("legacy_ar", "customers", None, tmp_path / "out" / "c.csv.gz", max_bytes=10_000_000)
    assert result.rows == 4 == result.source_count and result.columns[:2] == ["cust_id", "cust_name"]
    with gzip.open(result.path, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["cust_id", "cust_name", "zip", "amount", "created", "active", "notes"]
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["1"][1] == 'Müller & Söhne, "Ltd"' and by_id["1"][2] == "02134"          # accents, commas, quotes and the leading zero survive
    assert by_id["1"][3] == "10.50" and by_id["3"][3] == "1234567.89" and by_id["2"][3] == "7.00"   # numerics keep their scale, no exponent
    assert by_id["1"][4] == "2024-01-05 08:30:00" and by_id["2"][4] == ""                  # ISO timestamps; NULL is blank
    assert by_id["1"][5] == "true" and by_id["2"][5] == "false" and by_id["3"][5] == ""
    assert by_id["1"][6] == "line one\nline two"                                           # embedded newline survives the CSV
    sub = c.extract_csv("legacy_ar", "customers", ["cust_name", "cust_id"], tmp_path / "s.csv.gz", max_bytes=10_000_000)
    assert sub.columns == ["cust_name", "cust_id"]                                          # the chosen columns, in the chosen order


def test_extract_is_read_only_and_names_are_checked_against_the_catalog(pg, tmp_path):
    c = PostgresConnector(spec_of(pg))
    with c.session() as conn:                      # the session itself refuses writes, whatever the account may do
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            cur.execute("INSERT INTO legacy_ar.no_key VALUES ('p', 'q')")
    injection = ['customers"; DROP TABLE legacy_ar.customers; --', "customers; SELECT 1", 'x" OR "1"="1']
    for name in injection:
        with pytest.raises(ConnectorError, match="was not found"):
            c.extract_csv("legacy_ar", name, None, tmp_path / "i.csv.gz", 10_000_000)
        with pytest.raises(ConnectorError, match="was not found"):
            c.count("legacy_ar", name)
    with pytest.raises(ConnectorError, match="not in legacy_ar.customers"):
        c.extract_csv("legacy_ar", "customers", ['cust_id", (SELECT 1)) --'], tmp_path / "i.csv.gz", 10_000_000)
    assert c.count("legacy_ar", "customers") == 4                       # nothing was dropped
    odd = c.extract_csv("legacy_ar", "Odd Name", None, tmp_path / "odd.csv.gz", 10_000_000)   # unusual but legitimate identifiers quote correctly
    assert odd.columns == ["Col One", "select"] and odd.rows == 1


def test_extract_stops_at_the_size_cap_and_removes_the_partial_file(pg, tmp_path):
    c = PostgresConnector(spec_of(pg))
    with c.session() as conn:  # a table big enough to cross the cap: 60,000 rows of poorly compressible text
        cur = conn.cursor()
    big = psycopg2.connect(host=pg["host"], port=pg["port"], dbname=pg["database"], user=pg["user"], password=pg["password"])
    big.autocommit = True
    big.cursor().execute("CREATE TABLE legacy_ar.big AS SELECT g AS id, md5(g::text) || md5((g * 7)::text) AS payload FROM generate_series(1, 60000) g")
    big.close()
    target = tmp_path / "big.csv.gz"
    with pytest.raises(ConnectorError, match="larger than the"):
        c.extract_csv("legacy_ar", "big", None, target, max_bytes=50_000)
    assert not target.exists()
    ok = c.extract_csv("legacy_ar", "big", None, target, max_bytes=50_000_000)
    assert ok.rows == 60000 == ok.source_count


def test_preview_limits_and_formats_rows(pg):
    cols, rows = PostgresConnector(spec_of(pg)).preview("legacy_ar", "customers", limit=2)
    assert len(rows) == 2 and len(cols) == 7 and all(isinstance(v, str) for r in rows for v in r)


# ------------------------------------------------------------------ text conversion
def test_values_become_exact_text():
    import datetime as dt
    assert to_text(None) == "" and to_text(True) == "true" and to_text(False) == "false"
    assert to_text(Decimal("1E+3")) == "1000" and to_text(Decimal("10.50")) == "10.50" and to_text(Decimal("0.000001")) == "0.000001"
    assert to_text(dt.datetime(2024, 1, 5, 8, 30)) == "2024-01-05 08:30:00" and to_text(dt.date(2024, 2, 29)) == "2024-02-29" and to_text(dt.time(7, 5)) == "07:05:00"
    assert to_text(b"\x00\xff") == "00ff" and to_text(memoryview(b"ab")) == "6162" and to_text(12) == "12" and to_text("x") == "x"


# ------------------------------------------------------------------ connection registry and the API
def test_registry_validation_and_secret_handling(tmp_path):
    store = ConnectionStore(tmp_path, allowed_hosts=("db.internal",), env={"ORA_PW": "from-env"})
    good = dict(name="ora1", dialect="oracle", host="db.internal", port=1521, database="ORADB1", user="mig_ro", password_env="ORA_PW")
    rec = store.save(good, "alice", password="typed-pw")
    assert rec.created_by == "alice" and store.password_source(rec) == "memory"
    saved = (tmp_path / "connections.json").read_text(encoding="utf-8")
    assert "typed-pw" not in saved and "from-env" not in saved and "mig_ro" in saved           # nothing secret is written to disk
    assert store.spec_for("ora1").password == "typed-pw"
    fresh = ConnectionStore(tmp_path, allowed_hosts=("db.internal",), env={"ORA_PW": "from-env"})   # "after a restart": memory is gone, env still works
    assert fresh.password_source(fresh.get("ora1")) == "env:ORA_PW" and fresh.spec_for("ora1").password == "from-env"
    nopw = ConnectionStore(tmp_path, allowed_hosts=("db.internal",), env={})
    with pytest.raises(ConnectorError, match="no password is available"):
        nopw.spec_for("ora1")
    for bad, why in [({"name": "a b"}, "name"), ({"dialect": "db2"}, "connection type"), ({"host": "http://evil/x"}, "host"), ({"host": "other.example"}, "allowed list"),
                     ({"port": 70000}, "port"), ({"user": ""}, "user"), ({"password_env": "1 BAD"}, "environment variable"), ({"options": {"drop": 1}}, "unknown option"),
                     ({"database": ""}, "service name")]:
        with pytest.raises(ConnectionInputError, match=why):
            store.validate({**good, **bad})
    assert store.delete("ora1") and not store.delete("ora1") and store.get("ora1") is None


def _save(client, pg, name="legacy_pg", **extra):
    body = {"name": name, "dialect": "postgresql", "host": pg["host"], "port": pg["port"], "database": pg["database"], "user": pg["user"], "password": pg["password"], **extra}
    return client.post("/api/connections", json=body)


def test_connections_api_saves_tests_and_browses_without_ever_returning_the_password(client, pg, ctx):
    kinds = client.get("/api/connections/dialects").json()
    assert {d["dialect"] for d in kinds if d["family"] == "database"} == {"postgresql", "oracle", "mssql", "mysql"}
    assert {d["dialect"] for d in kinds if d["family"] == "storage"} == {"azure_blob", "s3", "sftp"}
    r = _save(client, pg)
    assert r.status_code == 201, r.text
    assert pg["password"] not in r.text and r.json()["password_source"] == "memory" and r.json()["created_by"] == "alice"
    assert pg["password"] not in client.get("/api/connections").text and pg["password"] not in (ctx.workspace / "connections.json").read_text(encoding="utf-8")

    t = client.post("/api/connections/test", json={"name": "legacy_pg"}).json()
    assert t["ok"] and t["server_version"] and t["schemas"] >= 1
    inline = client.post("/api/connections/test", json={"dialect": "postgresql", "host": pg["host"], "port": pg["port"], "database": pg["database"], "user": pg["user"], "password": "wrong"}).json()
    assert not inline["ok"] and "wrong" not in inline["detail"] and "rejected" in inline["detail"]

    assert "legacy_ar" in client.get("/api/connections/legacy_pg/schemas").json()
    tables = client.get("/api/connections/legacy_pg/tables", params={"schema_name": "legacy_ar", "views": "false"}).json()
    assert {x["name"] for x in tables} == {"customers", "order_lines", "no_key", "Odd Name"} and all(x["kind"] == "TABLE" for x in tables)
    cols = client.get("/api/connections/legacy_pg/columns", params={"schema_name": "legacy_ar", "table": "customers"}).json()
    assert cols[0]["name"] == "cust_id" and cols[0]["is_primary_key"] is True
    pv = client.get("/api/connections/legacy_pg/preview", params={"schema_name": "legacy_ar", "table": "customers", "limit": 3}).json()
    assert len(pv["rows"]) == 3 and pv["columns"][1]["name"] == "cust_name"

    assert client.get("/api/connections/ghost/schemas").status_code == 404
    assert client.get("/api/connections/legacy_pg/columns", params={"schema_name": "legacy_ar", "table": "nope"}).status_code == 502
    assert client.delete("/api/connections/legacy_pg").status_code == 200 and client.delete("/api/connections/legacy_pg").status_code == 404
    assert client.post("/api/connections", json={"name": "x", "dialect": "postgresql", "host": "a b", "user": "u"}).status_code == 422


def test_a_restart_forgets_the_password_and_the_api_says_so(client, pg, ctx):
    assert _save(client, pg).status_code == 201
    ctx.connections._passwords.clear()                                     # what a restart does
    r = client.get("/api/connections/legacy_pg/schemas")
    assert r.status_code == 409 and "no password is available" in r.json()["detail"]
    assert client.get("/api/connections").json()[0]["password_source"] == "missing"
    again = client.post("/api/connections/test", json={"name": "legacy_pg", "password": pg["password"]}).json()   # a fresh password can be supplied for one test
    assert again["ok"]


def test_only_connection_admins_may_manage_connections_and_hosts_can_be_restricted(client, pg, ctx):
    ctx.settings = Settings(**{**ctx.settings.__dict__, "connection_admins": ("dba",), "db_allowed_hosts": ("db.internal",)})
    ctx.connections = ConnectionStore(ctx.workspace, allowed_hosts=ctx.settings.db_allowed_hosts)
    denied = _save(client, pg)
    assert denied.status_code == 403 and "not allowed to manage database connections" in denied.json()["detail"]
    allowed = client.post("/api/connections", headers={"X-User": "dba"}, json={"name": "x", "dialect": "postgresql", "host": pg["host"], "database": "d", "user": "u"})
    assert allowed.status_code == 422 and "allowed list" in allowed.json()["detail"]


# ------------------------------------------------------------------ importing a table
def test_import_a_database_table_end_to_end(client, pg, ctx, session_factory):
    assert _save(client, pg).status_code == 201
    r = client.post("/api/sources/import-db", json={"connection": "legacy_pg", "schema_name": "legacy_ar", "table": "customers", "business_name": "AR customers"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rows_landed"] == 4 and body["control_status"] == "MATCH" and body["system_name"] == "legacy_pg" and body["table_name"] == "customers"
    assert body["origin"]["kind"] == "database" and body["origin"]["schema"] == "legacy_ar" and body["origin"]["source_row_count"] == 4 and body["origin"]["dialect"] == "postgresql"
    assert pg["password"] not in r.text

    snap = client.get(f"/api/tables/{body['table_id']}/snapshot").json()
    assert snap["origin"]["table"] == "customers" and snap["expected_rows"] == 4 and snap["original_filename"] == "legacy_ar.customers.csv.gz"
    listed = {t["table_name"]: t for t in client.get("/api/tables").json()}["customers"]
    assert listed["system_type"] == "POSTGRESQL" and listed["rows_landed"] == 4 and listed["has_source_file"]
    with session_factory() as s:
        source = s.query(CatDataSource).filter_by(system_name="legacy_pg").one()
        assert source.connection_config["host"] == pg["host"] and "password" not in json.dumps(source.connection_config)
        keys = [c.column_name for c in s.query(CatTable).filter_by(table_name="customers").one().columns if c.is_primary_key]
    assert keys == ["cust_id"]                                                             # the primary key came from the database

    profile = client.post(f"/api/profile/{body['table_id']}").json()                       # the extract is profiled like any other file
    assert profile["total_rows"] == 4
    types = {c["column_name"]: c["detected_type"] for c in profile["columns"]}
    assert types["cust_id"] == "BIGINT" and types["zip"] == "VARCHAR" and types["amount"] == "DOUBLE" and types["created"] == "TIMESTAMP"
    zips = {v["value"] for c in profile["columns"] if c["column_name"] == "zip" for v in c["top_values"]}
    assert "02134" in zips                                                                 # the leading zero survived the round trip

    again = client.post("/api/sources/import-db", json={"connection": "legacy_pg", "schema_name": "legacy_ar", "table": "customers"})
    assert again.status_code == 201 and again.json()["table_id"] == body["table_id"]       # same table name = replaced snapshot, not a duplicate


def test_import_options_composite_keys_column_subsets_and_rejections(client, pg, ctx):
    assert _save(client, pg).status_code == 201
    imp = lambda **kw: client.post("/api/sources/import-db", json={"connection": "legacy_pg", "schema_name": "legacy_ar", **kw})  # noqa: E731
    lines = imp(table="order_lines", system_name="ERP_DB", table_name="ORDER_LINES")
    assert lines.status_code == 201 and lines.json()["system_name"] == "ERP_DB" and lines.json()["table_name"] == "ORDER_LINES"
    subset = imp(table="customers", columns=["cust_name", "zip"], table_name="CUST_SUBSET")
    assert subset.status_code == 201 and subset.json()["columns_registered"] == 2          # the key column was not selected, so none is declared
    no_key = imp(table="no_key")
    assert no_key.status_code == 201                                                       # no key defined in the database: allowed, nothing to check
    forced = imp(table="no_key", table_name="FORCED", primary_key_columns=["a"])
    assert forced.status_code == 422 and "does not identify rows uniquely" in forced.json()["detail"]   # a declared key that repeats is refused
    assert imp(table="nope").status_code == 502 and "was not found" in imp(table="nope").json()["detail"]
    assert imp(table="customers", columns=["ghost"]).status_code == 502
    assert imp(table="customers", primary_key_columns=["ghost"]).status_code == 422
    assert client.post("/api/sources/import-db", json={"connection": "ghost", "schema_name": "s", "table": "t"}).status_code == 404
    assert not list((ctx.workspace / "tmp").glob("*"))                                      # scratch is always cleaned up

    empty = psycopg2.connect(host=pg["host"], port=pg["port"], dbname=pg["database"], user=pg["user"], password=pg["password"])
    empty.autocommit = True
    empty.cursor().execute("CREATE TABLE legacy_ar.empty_t (a int)")
    empty.close()
    nothing = imp(table="empty_t")
    assert nothing.status_code == 422 and "no rows to import" in nothing.json()["detail"]


def test_a_control_total_mismatch_rejects_the_import_and_leaves_nothing_behind(client, pg, ctx, session_factory, monkeypatch):
    assert _save(client, pg).status_code == 201
    real = PostgresConnector.extract_csv

    def lying(self, schema, table, columns, out_path, max_bytes, progress=None):
        result = real(self, schema, table, columns, out_path, max_bytes, progress)
        result.source_count += 3          # the source "changed": the count says 7 but 4 rows were read
        return result
    monkeypatch.setattr(PostgresConnector, "extract_csv", lying)
    r = client.post("/api/sources/import-db", json={"connection": "legacy_pg", "schema_name": "legacy_ar", "table": "customers"})
    assert r.status_code == 422 and "control total mismatch" in r.json()["detail"]
    with session_factory() as s:
        assert s.query(CatTable).count() == 0 and s.query(CatDataSource).count() == 0
    ok = client.post("/api/sources/import-db", json={"connection": "legacy_pg", "schema_name": "legacy_ar", "table": "customers", "control_total": False})
    assert ok.status_code == 201 and ok.json()["control_status"] == "NOT_PROVIDED"        # switching the control off is explicit and visible


# ------------------------------------------------------------------ the other dialects, with a fake DB-API connection
class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description, self._rows, self.arraysize = conn, None, [], 100

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, params))
        if "snapshot_isolation_state" in sql:                        # SQL Server asks whether snapshot isolation is on; here it is off
            self.description, self._rows = [("s",)], [(0,)]
            return
        for needle, (names, rows) in sorted(self.conn.answers.items(), key=lambda kv: kv[0] != "COUNT(*)"):   # the count query also contains the table name
            if needle in sql:
                self.description = [(n,) for n in names]
                self._rows = list(rows)
                return
        self.description, self._rows = None, []

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def close(self):
        pass


class FakeConn:
    version = "19.3.0.0.0"

    def __init__(self, answers):
        self.answers, self.executed, self.closed, self.rolled_back = answers, [], False, False

    def cursor(self, *a, **k):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _meta_answers(table="CUSTOMERS", cols=(("ID", "NUMBER(10,0)", None, 0, 1, 1), ("NAME", "VARCHAR2(40)", 40, 1, 0, 2))):
    return {"all_tables": (["n", "k", "r"], [(table, "TABLE", 3)]), "FROM sys.tables": (["n", "k", "r"], [(table, "TABLE", 3)]),
            "information_schema.tables": (["n", "k", "r"], [(table, "TABLE", 3)]), "all_tab_columns": (["a"] * 6, list(cols)),
            "INFORMATION_SCHEMA.COLUMNS": (["a"] * 6, list(cols)), "information_schema.columns": (["a"] * 6, list(cols)),
            'FROM "SALES"."CUSTOMERS"': (["ID", "NAME"], [(1, "A"), (2, "B"), (3, "C")]), "FROM [dbo].[CUSTOMERS]": (["ID", "NAME"], [(1, "A"), (2, "B"), (3, "C")]),
            "FROM `sales`.`CUSTOMERS`": (["ID", "NAME"], [(1, "A"), (2, "B"), (3, "C")]), "COUNT(*)": (["c"], [(3,)])}


@pytest.mark.parametrize("cls,schema,quoted", [(OracleConnector, "SALES", '"SALES"."CUSTOMERS"'), (MssqlConnector, "dbo", "[dbo].[CUSTOMERS]"), (MysqlConnector, "sales", "`sales`.`CUSTOMERS`")])
def test_other_dialects_browse_extract_and_stay_read_only_with_a_fake_connection(cls, schema, quoted, tmp_path):
    conn = FakeConn(_meta_answers())
    c = cls(ConnectionSpec(cls.dialect, "h", cls.default_port, "db", "u", "pw"), connect=lambda: conn)
    assert [t.name for t in c.tables(schema)] == ["CUSTOMERS"]
    cols = c.columns(schema, "CUSTOMERS")
    assert [(x.name, x.is_primary_key, x.nullable) for x in cols] == [("ID", True, False), ("NAME", False, True)]
    result = c.extract_csv(schema, "CUSTOMERS", None, tmp_path / "x.csv.gz", 1_000_000)
    assert result.rows == 3 == result.source_count and result.columns == ["ID", "NAME"]
    with gzip.open(result.path, "rt", encoding="utf-8") as fh:
        assert fh.read().splitlines() == ["ID,NAME", "1,A", "2,B", "3,C"]
    sql_text = " ".join(s for s, _ in conn.executed)
    assert f"SELECT COUNT(*) FROM {quoted}" in sql_text and "INSERT" not in sql_text.upper() and "UPDATE" not in sql_text.upper() and "DELETE" not in sql_text.upper()
    assert conn.closed and (conn.rolled_back or cls is MysqlConnector)                     # never commits, always closes; MySQL just closes (the server rolls back) so a half-streamed table is not read to its end
    with pytest.raises(ConnectorError, match="was not found"):
        c.extract_csv(schema, 'CUSTOMERS"; DROP TABLE x; --', None, tmp_path / "y.csv.gz", 1_000_000)


def test_dialect_specific_quoting_limits_and_read_only_statements():
    o, m, q = (cls(ConnectionSpec(cls.dialect, "h", 1, "d", "u", "p")) for cls in (OracleConnector, MssqlConnector, MysqlConnector))
    assert o.quote('A"B') == '"A""B"' and m.quote("a]b") == "[a]]b]" and q.quote("a`b") == "`a``b`"
    assert o._limit_sql('SELECT "A" FROM "T"', 5) == 'SELECT * FROM (SELECT "A" FROM "T") WHERE ROWNUM <= 5'
    assert m._limit_sql("SELECT [A] FROM [T]", 5) == "SELECT TOP (5) [A] FROM [T]" and q._limit_sql("SELECT `A` FROM `T`", 5) == "SELECT `A` FROM `T` LIMIT 5"
    conn = FakeConn({})
    o._begin_read_only(conn)
    q._begin_read_only(conn)
    assert [s for s, _ in conn.executed] == ["SET TRANSACTION READ ONLY", "SET SESSION TRANSACTION READ ONLY", "START TRANSACTION WITH CONSISTENT SNAPSHOT"]
    assert set(CONNECTORS) == {"postgresql", "oracle", "mssql", "mysql"} and {c.system_type for c in CONNECTORS.values()} == {"POSTGRESQL", "ORACLE", "MSSQL", "MYSQL"}
    with pytest.raises(ConnectorError, match="unsupported"):
        connector_for(ConnectionSpec("db2", "h", 1, "d", "u", "p"))
