"""Database TARGET connections: roles, write probes, DDL per dialect, and delivering a signed-off run into a table.

PostgreSQL is exercised against the real test server (schema `mig_target`). Oracle, SQL Server and MySQL are tested with a fake DB-API
connection: DDL text, placeholders, case, and the all-or-nothing behaviour are proven; they have not run against real servers.
"""

from __future__ import annotations

import gzip
import json

import psycopg2
import pytest

from src.connectors.base import ColumnDef, ConnectionSpec, ConnectorError
from src.connectors.dialects import MssqlConnector, MysqlConnector, OracleConnector, PostgresConnector
from src.services.lookup_service import COUNTRY_SEED, PAYMENT_TERMS_SEED
from tests.test_module7_api import HEADER, STEWARD, U, client, ctx, dataset, encoder, env, make_ctx, subset, upload  # noqa: F401


# ------------------------------------------------------------------ a real PostgreSQL target
@pytest.fixture()
def pg_target(engine):
    url = engine.url
    p = dict(host=url.host or "localhost", port=url.port or 5432, database=url.database, user=url.username, password=url.password or "")
    conn = psycopg2.connect(host=p["host"], port=p["port"], dbname=p["database"], user=p["user"], password=p["password"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS mig_target CASCADE; CREATE SCHEMA mig_target;")
    yield p, conn
    cur.execute("DROP SCHEMA IF EXISTS mig_target CASCADE")
    conn.close()


def save_target(client, p, name="ora_like_target", **extra):
    body = {"name": name, "dialect": "postgresql", "host": p["host"], "port": p["port"], "database": p["database"], "user": p["user"],
            "password": p["password"], "role": "target", "default_schema": "mig_target", **extra}
    return client.post("/api/connections", json=body)


def pg_query(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


# ------------------------------------------------------------------ a run that is signed off and ready for load
@pytest.fixture()
def ready_run(client, ctx, dataset):
    clean = subset(dataset, 300, clean=True)
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    reg = upload(client, clean)
    assert reg.status_code == 201, reg.text
    table_id = reg.json()["table_id"]
    assert client.post(f"/api/profile/{table_id}").status_code == 200
    mapping = client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
    rules = [("BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME", None, None), ("ADRC", "STREET", "DIRECT_COPY", "ADDR_STREET", None, None),
             ("ADRC", "CITY1", "DIRECT_COPY", "CITY", None, None), ("ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", None, COUNTRY_SEED),
             ("ADRC", "POST_CODE1", "DIRECT_COPY", "POSTAL_CODE", None, None), ("KNB1", "ZTERM", "VALUE_MAPPING", "PAYMENT_TERMS", None, PAYMENT_TERMS_SEED),
             ("BUT000", "BU_GROUP", "STATIC_VALUE", None, "'BP01'", None), ("BUT000", "TYPE", "STATIC_VALUE", None, "'2'", None)]
    for tt, tc, rt, src, logic, lookups in rules:
        r = client.post(f"/api/mapping/{mapping}/rules", json={"target_table": tt, "target_column": tc, "rule_type": rt, "source_column": src,
                                                               "transformation_logic": logic, "lookups": lookups})
        assert r.status_code == 201, r.text
    assert client.post(f"/api/governance/submit-mapping/{mapping}").status_code == 200
    assert client.post(f"/api/governance/approve-mapping/{mapping}", headers=U(STEWARD)).status_code == 200
    run = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "DB_TARGET"})
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["readiness"]["state"] == "READY_FOR_LOAD", body["readiness"]
    return {"run_id": body["run_id"], "valid": body["validation"]["valid_records"], "mapping": mapping}


def sign_off(client, run_id):
    r = client.post(f"/api/governance/sign-off-load/{run_id}", headers=U(STEWARD))
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ roles and the write probe
def test_target_connections_are_a_separate_role_with_a_write_probe(client, pg_target):
    p, _ = pg_target
    assert save_target(client, p).status_code == 201
    assert client.post("/api/connections", json={"name": "src1", "dialect": "postgresql", "host": p["host"], "port": p["port"], "database": p["database"],
                                                 "user": p["user"], "password": p["password"]}).status_code == 201
    assert [c["name"] for c in client.get("/api/connections", params={"role": "target"}).json()] == ["ora_like_target"]
    assert [c["name"] for c in client.get("/api/connections", params={"role": "source"}).json()] == ["src1"]
    listed = client.get("/api/connections", params={"role": "target"}).json()[0]
    assert listed["role"] == "target" and listed["default_schema"] == "mig_target" and p["password"] not in json.dumps(listed)
    targets = client.get("/api/targets").json()
    assert [d["name"] for d in targets["databases"]] == ["ora_like_target"]

    ok = client.post("/api/connections/ora_like_target/test-write", json={"schema_name": "mig_target"}).json()
    assert ok["ok"] and "create, insert, read and drop" in ok["detail"]
    assert pg_query(pg_target[1], "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0   # the probe table is gone
    assert not client.post("/api/connections/ora_like_target/test-write", json={"schema_name": "no_such_schema"}).json()["ok"]
    assert client.post("/api/connections/src1/test-write", json={"schema_name": "mig_target"}).status_code == 409   # sources are never written to
    assert client.post("/api/connections/ghost/test-write", json={"schema_name": "x"}).status_code == 404
    # a target connection cannot be used to import, and a source cannot be loaded into
    refused = client.post("/api/sources/import-db", json={"connection": "ora_like_target", "schema_name": "mig_target", "table": "t"})
    assert refused.status_code == 409 and "target connection" in refused.json()["detail"]
    assert client.post("/api/connections", json={"name": "x", "dialect": "postgresql", "host": "h", "user": "u", "database": "d", "role": "admin"}).status_code == 422


def test_read_only_accounts_cannot_be_written_to_and_say_so(client, pg_target):
    p, conn = pg_target
    cur = conn.cursor()
    cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mig_reader') THEN CREATE ROLE mig_reader LOGIN PASSWORD 'rd_pw'; END IF; END $$")
    cur.execute("GRANT USAGE ON SCHEMA mig_target TO mig_reader")
    cur.execute(f'GRANT CONNECT ON DATABASE "{p["database"]}" TO mig_reader')
    assert save_target(client, p, name="ro_target", user="mig_reader", password="rd_pw").status_code == 201
    try:
        res = client.post("/api/connections/ro_target/test-write", json={"schema_name": "mig_target"}).json()
        assert not res["ok"] and ("cannot write" in res["detail"] or "refused" in res["detail"]), res
        assert pg_query(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0
    finally:
        cur.execute("DROP OWNED BY mig_reader")
        cur.execute("DROP ROLE IF EXISTS mig_reader")


# ------------------------------------------------------------------ delivering a run
def test_a_signed_off_run_is_delivered_into_postgres_exactly_and_the_certificate_checks_it(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    assert save_target(client, p).status_code == 201
    asked = {"connection": "ora_like_target", "table_name": "MIG_BP_RUN"}

    # the preview shows exactly what would be created, and creates nothing
    pv = client.get(f"/api/pipeline/load-db/{run_id}/preview", params=asked)
    assert pv.status_code == 200, pv.text
    plan = pv.json()
    names = [c["name"] for c in plan["columns"]]
    assert names[:2] == ["legacy_key", "mig_run_id"] and "but000_name_org1" in names and "adrc_country" in names and "knb1_zterm" in names
    assert not any(n.startswith("src_") or n.startswith("_") for n in names) and plan["rows"] == valid and plan["schema_name"] == "mig_target"
    types = {c["name"]: c["type"] for c in plan["columns"]}
    assert types["legacy_key"] == "VARCHAR(255)" and types["mig_run_id"] == "INTEGER" and types["but000_name_org1"] == "VARCHAR(40)"
    assert 'PRIMARY KEY ("legacy_key")' in plan["ddl"] and 'CREATE TABLE "mig_target"."mig_bp_run"' in plan["ddl"]
    assert pg_query(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0

    # the gate: no sign-off, no delivery
    blocked = client.post(f"/api/pipeline/load-db/{run_id}", json=asked)
    assert blocked.status_code == 409 and "not been signed off" in blocked.json()["detail"]
    sign_off(client, run_id)

    for bad in ("1starts_with_digit", "has space", 'x"; DROP TABLE t; --', "a" * 31):
        assert client.post(f"/api/pipeline/load-db/{run_id}", json={**asked, "table_name": bad}).status_code in (422,)
    assert client.post(f"/api/pipeline/load-db/{run_id}", json={**asked, "connection": "ghost"}).status_code == 404
    assert pg_query(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0   # rejected requests created nothing

    done = client.post(f"/api/pipeline/load-db/{run_id}", json=asked)
    assert done.status_code == 201, done.text
    d = done.json()
    assert d["rows_loaded"] == d["rows_in_target"] == d["valid_records"] == valid and d["table"] == "mig_bp_run" and d["loaded_by"] == "alice" and not d["replaced_existing"]

    # the table really holds the validated records, with the SAP lengths and a primary key
    assert pg_query(conn, 'SELECT count(*), count(distinct legacy_key) FROM mig_target.mig_bp_run')[0] == (valid, valid)
    assert pg_query(conn, "SELECT character_maximum_length FROM information_schema.columns WHERE table_schema='mig_target' AND table_name='mig_bp_run' AND column_name='but000_name_org1'")[0][0] == 40
    assert pg_query(conn, "SELECT count(*) FROM information_schema.table_constraints WHERE table_schema='mig_target' AND table_name='mig_bp_run' AND constraint_type='PRIMARY KEY'")[0][0] == 1
    import duckdb
    payload = duckdb.connect().execute("SELECT CAST(_SRC_PK AS VARCHAR), BUT000_NAME_ORG1, ADRC_COUNTRY, KNB1_ZTERM, ADRC_POST_CODE1 FROM read_parquet(?) ORDER BY 1", [ctx.load_validation_report(run_id).valid_path]).fetchall()
    stored = pg_query(conn, "SELECT legacy_key, but000_name_org1, adrc_country, knb1_zterm, adrc_post_code1 FROM mig_target.mig_bp_run ORDER BY 1")
    assert [tuple(r) for r in stored] == [tuple(r) for r in payload]                    # every value identical, including leading zeros and accents
    assert {r[0] for r in pg_query(conn, "SELECT DISTINCT mig_run_id FROM mig_target.mig_bp_run")} == {run_id}

    # a delivery is once per table name; replace is explicit
    again = client.post(f"/api/pipeline/load-db/{run_id}", json=asked)
    assert again.status_code == 422 and "already exists" in again.json()["detail"]
    assert pg_query(conn, "SELECT count(*) FROM mig_target.mig_bp_run")[0][0] == valid          # the refused attempt changed nothing
    replaced = client.post(f"/api/pipeline/load-db/{run_id}", json={**asked, "mode": "replace", "include_source_columns": True})
    assert replaced.status_code == 201 and replaced.json()["replaced_existing"] and replaced.json()["include_source_columns"]
    cols = {r[0] for r in pg_query(conn, "SELECT column_name FROM information_schema.columns WHERE table_schema='mig_target' AND table_name='mig_bp_run'")}
    assert {"src_cust_name", "src_country"} <= cols
    assert len(client.get(f"/api/pipeline/load-db/{run_id}").json()) == 2

    # the evidence is in the run folder (no secrets) and the certificate checks the target's own count
    evidence = (ctx.run_dir(run_id) / "db_loads.json").read_text(encoding="utf-8")
    assert p["password"] not in evidence and "mig_bp_run" in evidence
    checks = {c["name"]: c for c in client.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}
    delivery = [c for n, c in checks.items() if n.startswith("db_delivery_postgresql_mig_target.mig_bp_run")]
    assert len(delivery) == 1 and delivery[0]["passed"] and f"{valid} row(s) counted" in delivery[0]["detail"]   # the replaced delivery is superseded, not counted twice


def test_a_failed_delivery_leaves_no_table_behind(client, ctx, pg_target, ready_run, monkeypatch):
    p, conn = pg_target
    assert save_target(client, p).status_code == 201
    sign_off(client, ready_run["run_id"])
    calls = {"n": 0}
    real = PostgresConnector._insert_many

    def flaky(self, cur, sql, batch):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection lost mid-load")
        real(self, cur, sql, batch)
    monkeypatch.setattr(PostgresConnector, "_insert_many", flaky)
    r = client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json={"connection": "ora_like_target", "table_name": "MIG_FAIL", "batch_size": 50})
    assert r.status_code == 422 and "rolled back" in r.json()["detail"] and calls["n"] == 2
    assert pg_query(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0    # table and rows are both gone
    assert not (ctx.run_dir(ready_run["run_id"]) / "db_loads.json").exists()                                 # no evidence of a delivery that did not happen
    monkeypatch.setattr(PostgresConnector, "_insert_many", real)
    assert client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json={"connection": "ora_like_target", "table_name": "MIG_FAIL"}).status_code == 201


def test_a_source_connection_cannot_be_loaded_into(client, pg_target, ready_run):
    p, _ = pg_target
    assert client.post("/api/connections", json={"name": "src1", "dialect": "postgresql", "host": p["host"], "port": p["port"], "database": p["database"],
                                                 "user": p["user"], "password": p["password"], "default_schema": "mig_target"}).status_code == 201
    sign_off(client, ready_run["run_id"])
    r = client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json={"connection": "src1", "schema_name": "mig_target", "table_name": "T"})
    assert r.status_code == 409 and "source connection" in r.json()["detail"]
    save = client.post("/api/connections", json={"name": "t2", "dialect": "postgresql", "host": p["host"], "port": p["port"], "database": p["database"],
                                                 "user": p["user"], "password": p["password"], "role": "target"})
    nodefault = client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json={"connection": "t2", "table_name": "T"})
    assert save.status_code == 201 and nodefault.status_code == 422 and "no schema" in nodefault.json()["detail"]


# ------------------------------------------------------------------ the other dialects, with a fake connection
class FakeCur:
    def __init__(self, conn):
        self.conn, self.description = conn, None
        self._rows = []

    def execute(self, sql, params=()):
        self.conn.executed.append(sql)
        if sql.startswith("INSERT INTO") and params:          # SQL Server sends one multi-row INSERT per chunk instead of executemany
            if self.conn.fail_inserts:
                raise RuntimeError("disk full")
            width = sql.split(" VALUES ", 1)[1].split(")", 1)[0].count("%s")
            self.conn.inserted.extend(tuple(params[i:i + width]) for i in range(0, len(params), width))
            return
        if "COUNT(*)" in sql:
            self._rows = [(self.conn.count if self.conn.count is not None else len(self.conn.inserted),)]
        elif "all_tables" in sql or "sys.tables" in sql or "information_schema.tables" in sql:
            self._rows = [(t, "TABLE", 0) for t in self.conn.tables]
        elif "all_users" in sql or "sys.schemas" in sql or "schema_name" in sql:
            self._rows = [(s,) for s in self.conn.schemas]
        else:
            self._rows = []
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")

    def executemany(self, sql, batch):
        self.conn.executed.append(sql)
        if self.conn.fail_inserts:
            raise RuntimeError("disk full")
        self.conn.inserted.extend(batch)

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def close(self):
        pass


class FakeTargetConn:
    def __init__(self, schemas=("APP",), tables=(), count=None, fail_inserts=False, fail_on=None):
        self.schemas, self.tables, self.count, self.fail_inserts, self.fail_on = list(schemas), list(tables), count, fail_inserts, fail_on
        self.executed, self.inserted, self.commits, self.rollbacks, self.closed = [], [], 0, 0, False

    def cursor(self, *a, **k):
        return FakeCur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


COLS = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("BUT000_NAME_ORG1", "text", 40)]
ROWS = [("1001", 7, "Müller GmbH"), ("1002", 7, None)]


def connector(cls, conn):
    return cls(ConnectionSpec(cls.dialect, "h", cls.default_port, "db", "u", "pw"), connect=lambda: conn)


@pytest.mark.parametrize("cls,schema,expect_ddl,placeholder", [
    (OracleConnector, "APP", ['CREATE TABLE "APP"."MIG_T"', '"LEGACY_KEY" VARCHAR2(255 CHAR) NOT NULL', '"MIG_RUN_ID" NUMBER(10) NOT NULL', '"BUT000_NAME_ORG1" VARCHAR2(40 CHAR)', 'PRIMARY KEY ("LEGACY_KEY")'], ":3"),
    (MssqlConnector, "dbo", ["CREATE TABLE [dbo].[MIG_T]", "[LEGACY_KEY] NVARCHAR(255) NOT NULL", "[MIG_RUN_ID] INT NOT NULL", "[BUT000_NAME_ORG1] NVARCHAR(40)", "PRIMARY KEY ([LEGACY_KEY])"], "%s"),
    (MysqlConnector, "app", ["CREATE TABLE `app`.`MIG_T`", "`LEGACY_KEY` VARCHAR(255) NOT NULL", "`MIG_RUN_ID` INT NOT NULL", "`BUT000_NAME_ORG1` VARCHAR(40)", "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"], "%s"),
])
def test_other_dialects_generate_native_ddl_and_load_all_or_nothing(cls, schema, expect_ddl, placeholder):
    conn = FakeTargetConn(schemas=(schema,))
    c = connector(cls, conn)
    out = c.load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS), batch_size=1, expected_rows=2)
    assert out.rows_inserted == out.rows_in_target == 2 and len(conn.inserted) == 2 and conn.commits == 1 and conn.closed
    for piece in expect_ddl:
        assert piece in out.ddl, (piece, out.ddl)
    assert any(placeholder in s and "INSERT INTO" in s for s in conn.executed)
    assert conn.inserted[0] == ("1001", 7, "Müller GmbH") and conn.inserted[1][2] is None


@pytest.mark.parametrize("cls,schema", [(OracleConnector, "APP"), (MysqlConnector, "app"), (MssqlConnector, "dbo")])
def test_failures_leave_nothing_behind_on_every_dialect(cls, schema):
    # inserts fail: transactional DDL (mssql) rolls back; Oracle / MySQL commit DDL at once, so the created table is dropped again
    conn = FakeTargetConn(schemas=(schema,), fail_inserts=True)
    c = connector(cls, conn)
    with pytest.raises(ConnectorError, match="rolled back"):
        c.load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS))
    assert conn.rollbacks >= 1 and conn.closed
    assert any(s.startswith("DROP TABLE") for s in conn.executed) == (not c.transactional_ddl)
    # a count that disagrees with what was sent is refused before commit
    conn2 = FakeTargetConn(schemas=(schema,), count=1)
    with pytest.raises(ConnectorError, match="row count check failed"):
        connector(cls, conn2).load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS))
    transactional = connector(cls, FakeTargetConn()).transactional_ddl
    assert conn2.commits == (0 if transactional else 1)                        # the only commit is the cleanup DROP where DDL cannot be rolled back
    assert any(x.startswith("DROP TABLE") for x in conn2.executed) == (not transactional)
    # an existing table is not touched unless replace is chosen
    existing = connector(cls, FakeTargetConn()).natural_case("MIG_T")
    conn3 = FakeTargetConn(schemas=(schema,), tables=[existing])
    with pytest.raises(ConnectorError, match="already exists"):
        connector(cls, conn3).load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS))
    assert not any(s.startswith("DROP TABLE") or s.startswith("CREATE TABLE") for s in conn3.executed)
    with pytest.raises(ConnectorError, match="schema 'nope' was not found"):
        connector(cls, FakeTargetConn(schemas=(schema,))).load_table("nope", "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS))
    for bad in ("1abc", "a b", 'x"y', "", "a" * 31):
        with pytest.raises(ConnectorError, match="name"):
            connector(cls, FakeTargetConn(schemas=(schema,))).load_table(schema, bad, COLS, ["LEGACY_KEY"], iter(ROWS))


def test_natural_case_and_wide_text_types():
    o, m, q, pg = (c(ConnectionSpec(c.dialect, "h", 1, "d", "u", "p")) for c in (OracleConnector, MssqlConnector, MysqlConnector, PostgresConnector))
    assert (o.natural_case("Mig_t"), pg.natural_case("Mig_T"), m.natural_case("Mig_T"), q.natural_case("Mig_T")) == ("MIG_T", "mig_t", "Mig_T", "Mig_T")
    wide = ColumnDef("RAW", "text", 5000)
    assert o._type_sql(wide) == "CLOB" and m._type_sql(wide) == "NVARCHAR(MAX)" and q._type_sql(wide) == "VARCHAR(5000)" and pg._type_sql(wide) == "VARCHAR(5000)"
