"""The connectors against REAL MySQL, SQL Server and Oracle servers (Docker, see docker-compose.servers.yml).

    docker compose -f docker-compose.servers.yml --profile mysql up -d --wait
    python -m pytest tests/test_real_servers.py -k mysql

A server that is not reachable is skipped, so the normal suite stays green without Docker. Each server gets a read-only login for the
source tests and a write login for the target tests; the fixtures are created natively (not through our own code), so the tests check
what the connectors really read and write: unicode, NULLs, leading zeros, decimals, timestamps, large text, composite keys, views,
read-only enforcement, identifier safety, and all-or-nothing loading.
"""

from __future__ import annotations

import csv
import gzip
import socket
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import pytest

from src.connectors.base import ColumnDef, ConnectionSpec, ConnectorError
from src.connectors.dialects import connector_for
from tests.test_db_targets import ready_run, sign_off  # noqa: F401  (a signed-off run, ready for delivery)
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

ROWS = [  # id, name, zip, amount, created, active, notes
    (1, 'Müller & Söhne, "Ltd"', "02134", Decimal("10.50"), "2024-01-05 08:30:00", 1, "line one\nline two"),
    (2, "Café Ñandú", "10001", Decimal("7"), None, 0, None),
    (3, "Zoë AG", None, Decimal("1234567.89"), "2023-12-31 23:59:59", None, "x"),
    (4, "Acme", "80331", Decimal("0.10"), "2024-02-29 12:00:00", 1, "plain"),
]
SMALL_LINES = [(1, 1, "A-1"), (1, 2, "B-2"), (2, 1, "A-1")]


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


@dataclass
class Server:
    name: str
    dialect: str
    host: str
    port: int
    src_schema: str
    tgt_schema: str
    ro: tuple[str, str, str]           # user, password, database
    rw: tuple[str, str, str]
    setup: Callable[["Server"], None]

    def spec(self, role: str) -> ConnectionSpec:
        user, pw, db = self.ro if role == "ro" else self.rw
        return ConnectionSpec(self.dialect, self.host, self.port, db, user, pw, {"connect_timeout": 15})


# ------------------------------------------------------------------ MySQL
def setup_mysql(s: Server) -> None:
    import pymysql
    conn = pymysql.connect(host=s.host, port=s.port, user="root", password="Root_pw_Test1", charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    for sql in ["DROP DATABASE IF EXISTS legacy_test", "DROP DATABASE IF EXISTS legacy_tgt", "CREATE DATABASE legacy_test CHARACTER SET utf8mb4", "CREATE DATABASE legacy_tgt CHARACTER SET utf8mb4",
                """CREATE TABLE legacy_test.customers (cust_id INT PRIMARY KEY, cust_name VARCHAR(60) NOT NULL, zip VARCHAR(10), amount DECIMAL(12,2), created DATETIME,
                   active TINYINT(1), notes TEXT)""",
                "CREATE TABLE legacy_test.order_lines (order_id INT, line_no INT, sku VARCHAR(20), PRIMARY KEY (order_id, line_no))",
                "CREATE TABLE legacy_test.no_key (a VARCHAR(5), b VARCHAR(5))",
                "CREATE VIEW legacy_test.active_customers AS SELECT cust_id, cust_name FROM legacy_test.customers WHERE active = 1",
                "CREATE USER IF NOT EXISTS 'legacy_ro'@'%' IDENTIFIED BY 'Ro_pw_Test1'", "GRANT SELECT ON legacy_test.* TO 'legacy_ro'@'%'",
                "CREATE USER IF NOT EXISTS 'legacy_rw'@'%' IDENTIFIED BY 'Rw_pw_Test1'", "GRANT ALL ON legacy_tgt.* TO 'legacy_rw'@'%'"]:
        cur.execute(sql)
    cur.executemany("INSERT INTO legacy_test.customers VALUES (%s,%s,%s,%s,%s,%s,%s)", ROWS)
    cur.executemany("INSERT INTO legacy_test.order_lines VALUES (%s,%s,%s)", SMALL_LINES)
    cur.executemany("INSERT INTO legacy_test.no_key VALUES (%s,%s)", [("x", "y"), ("x", "y")])
    cur.execute("ANALYZE TABLE legacy_test.customers")
    conn.close()


# ------------------------------------------------------------------ SQL Server
def setup_mssql(s: Server) -> None:
    import pytds
    conn = pytds.connect(server=s.host, port=s.port, user="sa", password="Strong_Pw_123!", database="master", autocommit=True, login_timeout=20)
    cur = conn.cursor()
    cur.execute("IF DB_ID('legacy_test') IS NOT NULL BEGIN ALTER DATABASE legacy_test SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE legacy_test END")
    cur.execute("CREATE DATABASE legacy_test COLLATE Latin1_General_100_CI_AS_SC_UTF8")
    conn.close()
    conn = pytds.connect(server=s.host, port=s.port, user="sa", password="Strong_Pw_123!", database="legacy_test", autocommit=True, login_timeout=20)
    cur = conn.cursor()
    for sql in ["CREATE SCHEMA tgt",
                """CREATE TABLE dbo.customers (cust_id INT PRIMARY KEY, cust_name NVARCHAR(60) NOT NULL, zip VARCHAR(10), amount DECIMAL(12,2), created DATETIME2(0),
                   active BIT, notes NVARCHAR(MAX))""",
                "CREATE TABLE dbo.order_lines (order_id INT, line_no INT, sku VARCHAR(20), PRIMARY KEY (order_id, line_no))",
                "CREATE TABLE dbo.no_key (a VARCHAR(5), b VARCHAR(5))",
                "CREATE VIEW dbo.active_customers AS SELECT cust_id, cust_name FROM dbo.customers WHERE active = 1"]:
        cur.execute(sql)
    cur.executemany("INSERT INTO dbo.customers VALUES (%s,%s,%s,%s,%s,%s,%s)", ROWS)
    cur.executemany("INSERT INTO dbo.order_lines VALUES (%s,%s,%s)", SMALL_LINES)
    cur.executemany("INSERT INTO dbo.no_key VALUES (%s,%s)", [("x", "y"), ("x", "y")])
    conn.close()
    conn = pytds.connect(server=s.host, port=s.port, user="sa", password="Strong_Pw_123!", database="master", autocommit=True, login_timeout=20)
    cur = conn.cursor()
    for login, pw in (("legacy_ro", "Ro_pw_Test1!"), ("legacy_rw", "Rw_pw_Test1!")):
        cur.execute(f"IF SUSER_ID('{login}') IS NULL CREATE LOGIN {login} WITH PASSWORD = '{pw}', CHECK_POLICY = OFF")
    conn.close()
    conn = pytds.connect(server=s.host, port=s.port, user="sa", password="Strong_Pw_123!", database="legacy_test", autocommit=True, login_timeout=20)
    cur = conn.cursor()
    for sql in ["CREATE USER legacy_ro FOR LOGIN legacy_ro", "GRANT SELECT ON SCHEMA::dbo TO legacy_ro", "CREATE USER legacy_rw FOR LOGIN legacy_rw",
                "GRANT CREATE TABLE TO legacy_rw", "GRANT CONTROL ON SCHEMA::tgt TO legacy_rw"]:
        cur.execute(sql)
    conn.close()


# ------------------------------------------------------------------ Oracle
def setup_oracle(s: Server) -> None:
    import oracledb
    conn = oracledb.connect(user="system", password="Oracle_pw_Test1", dsn=f"{s.host}:{s.port}/FREEPDB1")
    cur = conn.cursor()
    for user in ("LEGACY_SRC", "LEGACY_RO", "LEGACY_TGT"):
        cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :1", (user,))
        if cur.fetchone()[0]:
            cur.execute(f"DROP USER {user} CASCADE")
    for sql in ["CREATE USER LEGACY_SRC IDENTIFIED BY Src_pw_Test1 QUOTA UNLIMITED ON USERS", "GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW TO LEGACY_SRC",
                "CREATE USER LEGACY_RO IDENTIFIED BY Ro_pw_Test1", "GRANT CREATE SESSION TO LEGACY_RO",
                "CREATE USER LEGACY_TGT IDENTIFIED BY Rw_pw_Test1 QUOTA UNLIMITED ON USERS", "GRANT CREATE SESSION, CREATE TABLE TO LEGACY_TGT"]:
        cur.execute(sql)
    conn.close()
    conn = oracledb.connect(user="LEGACY_SRC", password="Src_pw_Test1", dsn=f"{s.host}:{s.port}/FREEPDB1")
    cur = conn.cursor()
    for sql in ["""CREATE TABLE customers (cust_id NUMBER(10) PRIMARY KEY, cust_name VARCHAR2(60 CHAR) NOT NULL, zip VARCHAR2(10 CHAR), amount NUMBER(12,2), created TIMESTAMP(0),
                   active NUMBER(1), notes CLOB)""",
                "CREATE TABLE order_lines (order_id NUMBER(10), line_no NUMBER(10), sku VARCHAR2(20), PRIMARY KEY (order_id, line_no))",
                "CREATE TABLE no_key (a VARCHAR2(5), b VARCHAR2(5))",
                "CREATE VIEW active_customers AS SELECT cust_id, cust_name FROM customers WHERE active = 1"]:
        cur.execute(sql)
    import datetime as dt
    rows = [(r[0], r[1], r[2], r[3], dt.datetime.fromisoformat(r[4]) if r[4] else None, r[5], r[6]) for r in ROWS]
    cur.executemany("INSERT INTO customers VALUES (:1,:2,:3,:4,:5,:6,:7)", rows)
    cur.executemany("INSERT INTO order_lines VALUES (:1,:2,:3)", SMALL_LINES)
    cur.executemany("INSERT INTO no_key VALUES (:1,:2)", [("x", "y"), ("x", "y")])
    for obj in ("customers", "order_lines", "no_key", "active_customers"):
        cur.execute(f"GRANT SELECT ON {obj} TO LEGACY_RO")
    cur.callproc("DBMS_STATS.GATHER_TABLE_STATS", ["LEGACY_SRC", "CUSTOMERS"])
    conn.commit()
    conn.close()


SERVERS = {
    "mysql": Server("mysql", "mysql", "localhost", 33306, "legacy_test", "legacy_tgt", ("legacy_ro", "Ro_pw_Test1", "legacy_test"), ("legacy_rw", "Rw_pw_Test1", "legacy_tgt"), setup_mysql),
    "mssql": Server("mssql", "mssql", "localhost", 14333, "dbo", "tgt", ("legacy_ro", "Ro_pw_Test1!", "legacy_test"), ("legacy_rw", "Rw_pw_Test1!", "legacy_test"), setup_mssql),
    "oracle": Server("oracle", "oracle", "localhost", 15210, "LEGACY_SRC", "LEGACY_TGT", ("LEGACY_RO", "Ro_pw_Test1", "FREEPDB1"), ("LEGACY_TGT", "Rw_pw_Test1", "FREEPDB1"), setup_oracle),
}
CASE = {"mysql": str, "mssql": str, "oracle": str.upper}       # how each product spells the fixture names the tests ask for


@pytest.fixture(params=sorted(SERVERS))
def server(request):
    s = SERVERS[request.param]
    if not port_open(s.host, s.port):
        pytest.skip(f"{s.name} is not running on {s.host}:{s.port} (docker compose -f docker-compose.servers.yml --profile {s.name} up -d --wait)")
    s.setup(s)
    return s


def n(server: Server, name: str) -> str:
    return CASE[server.name](name)


def norm_ts(value: str) -> str:
    return value[:19] if value else value


# ------------------------------------------------------------------ source side
def test_connect_browse_and_describe(server):
    c = connector_for(server.spec("ro"))
    info = c.test()
    assert info["ok"] and info["server_version"]
    assert server.src_schema in c.schemas()
    tables = {t.name.lower(): t for t in c.tables(server.src_schema)}
    assert {"customers", "order_lines", "no_key", "active_customers"} <= set(tables)
    assert tables["active_customers"].kind == "VIEW" and tables["customers"].kind == "TABLE"
    assert [t.name.lower() for t in c.tables(server.src_schema, include_views=False, name_filter="CUST")] == ["customers"]

    cols = c.columns(server.src_schema, n(server, "customers"))
    by = {x.name.lower(): x for x in cols}
    assert [x.name.lower() for x in cols] == ["cust_id", "cust_name", "zip", "amount", "created", "active", "notes"]
    assert by["cust_id"].is_primary_key and not by["cust_name"].is_primary_key and not by["cust_name"].nullable and by["zip"].nullable
    assert by["cust_name"].length == 60 or "60" in by["cust_name"].data_type
    assert {x.name.lower() for x in c.columns(server.src_schema, n(server, "order_lines")) if x.is_primary_key} == {"order_id", "line_no"}
    assert not any(x.is_primary_key for x in c.columns(server.src_schema, n(server, "no_key")))
    with pytest.raises(ConnectorError, match="not found|cannot see"):
        c.columns(server.src_schema, "NOPE_NOT_HERE")


def test_wrong_password_and_unreachable_are_clear_and_do_not_echo_the_password(server):
    user, _, db = server.ro
    from src.connectors.base import ConnectorAuthError
    with pytest.raises(ConnectorError) as bad:
        connector_for(ConnectionSpec(server.dialect, server.host, server.port, db, user, "definitely-wrong-pw", {"connect_timeout": 10})).test()
    assert "definitely-wrong-pw" not in str(bad.value) and isinstance(bad.value, ConnectorAuthError), str(bad.value)
    with pytest.raises(ConnectorError, match="cannot connect"):
        connector_for(ConnectionSpec(server.dialect, "127.0.0.1", 1, db, user, "secret-pw", {"connect_timeout": 2})).test()


def test_extract_is_exact_counted_in_one_transaction_and_streamed(server, tmp_path):
    c = connector_for(server.spec("ro"))
    result = c.extract_csv(server.src_schema, n(server, "customers"), None, tmp_path / "c.csv.gz", max_bytes=10_000_000)
    assert result.rows == 4 == result.source_count
    with gzip.open(result.path, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header, data = [h.lower() for h in rows[0]], {r[0]: r for r in rows[1:]}
    assert header == ["cust_id", "cust_name", "zip", "amount", "created", "active", "notes"]
    assert data["1"][1] == 'Müller & Söhne, "Ltd"' and data["2"][1] == "Café Ñandú" and data["3"][1] == "Zoë AG"      # unicode, commas and quotes survive
    assert data["1"][2] == "02134" and data["3"][2] == ""                                                           # leading zero kept; NULL is blank
    assert [Decimal(data[k][3]) for k in ("1", "2", "3", "4")] == [Decimal("10.50"), Decimal("7"), Decimal("1234567.89"), Decimal("0.10")]   # exact, no float noise
    assert "e" not in data["3"][3].lower()
    assert norm_ts(data["1"][4]) == "2024-01-05 08:30:00" and data["2"][4] == "" and norm_ts(data["4"][4]) == "2024-02-29 12:00:00"
    assert data["1"][6] == "line one\nline two" and data["2"][6] == ""                                               # CLOB / TEXT / NVARCHAR(MAX) read as text, newline intact
    sub = c.extract_csv(server.src_schema, n(server, "customers"), [n(server, "cust_name"), n(server, "cust_id")], tmp_path / "s.csv.gz", max_bytes=10_000_000)
    assert [x.lower() for x in sub.columns] == ["cust_name", "cust_id"]


def test_the_source_session_is_read_only_and_names_are_checked(server, tmp_path):
    c = connector_for(server.spec("ro"))
    with c.session() as conn:
        cur = conn.cursor()
        with pytest.raises(Exception):                         # the account has no write right; the session is read-only where the product supports it
            cur.execute(f"INSERT INTO {c.quote(server.src_schema)}.{c.quote(n(server, 'no_key'))} VALUES ('p', 'q')")
    for evil in ['customers"; DROP TABLE x; --', "customers; SELECT 1", "x`; DROP TABLE y; --", "x]; DROP TABLE z; --"]:
        with pytest.raises(ConnectorError, match="not found|cannot see"):
            c.extract_csv(server.src_schema, evil, None, tmp_path / "i.csv.gz", 10_000_000)
    with pytest.raises(ConnectorError, match="not in"):
        c.extract_csv(server.src_schema, n(server, "customers"), ['cust_id") --'], tmp_path / "i.csv.gz", 10_000_000)
    assert c.count(server.src_schema, n(server, "customers")) == 4


def test_extract_stops_at_the_size_cap_and_removes_the_partial_file(server, tmp_path):
    # a dedicated wide table via the target connection, then read through the source connection after granting nothing extra:
    # use the writer to build it inside ITS schema and read it with the same login (the cap logic is what is under test)
    w = connector_for(server.spec("rw"))
    cols = [ColumnDef("ID", "int", nullable=False), ColumnDef("PAYLOAD", "text", 200)]
    rows = ((i, f"{i:08d}-" + "abcdefghij" * 15) for i in range(1, 40001))
    w.load_table(server.tgt_schema, "BIG_T", cols, ["ID"], rows, mode="replace", batch_size=2000)
    target = tmp_path / "big.csv.gz"
    with pytest.raises(ConnectorError, match="larger than the"):
        w.extract_csv(server.tgt_schema, w.natural_case("BIG_T"), None, target, max_bytes=20_000)
    assert not target.exists()
    ok = w.extract_csv(server.tgt_schema, w.natural_case("BIG_T"), None, target, max_bytes=50_000_000)
    assert ok.rows == 40000 == ok.source_count


# ------------------------------------------------------------------ target side
def test_probe_write_and_load_create_replace_exists_and_unicode_round_trip(server):
    w = connector_for(server.spec("rw"))
    assert "succeeded" in w.probe_write(server.tgt_schema)
    r = connector_for(server.spec("ro"))
    with pytest.raises(ConnectorError):                                   # the read-only login cannot write: the probe says so
        r.probe_write(server.src_schema)
    cols = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("BUT000_NAME_ORG1", "text", 40), ColumnDef("ADRC_POST_CODE1", "text", 10)]
    data = [("1001", 7, 'Müller & Söhne, "Ltd"', "02134"), ("1002", 7, "Café Ñandú", None), ("1003", 7, "日本語の名前", "1000001")]
    out = w.load_table(server.tgt_schema, "MIG_BP_T", cols, ["LEGACY_KEY"], iter(data), mode="create", expected_rows=3)
    assert out.rows_inserted == out.rows_in_target == 3 and not out.replaced_existing
    table = w.natural_case("MIG_BP_T")
    assert table in {t.name for t in w.tables(server.tgt_schema)}
    back = w.extract_csv(server.tgt_schema, table, None, __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "b.csv.gz", 1_000_000)
    with gzip.open(back.path, "rt", encoding="utf-8", newline="") as fh:
        got = {r_[0]: r_ for r_ in list(csv.reader(fh))[1:]}
    assert got["1001"][2:] == ['Müller & Söhne, "Ltd"', "02134"] and got["1002"][3] == "" and got["1003"][2] == "日本語の名前" and got["1003"][1] == "7"
    pk = [c_.name.lower() for c_ in w.columns(server.tgt_schema, table) if c_.is_primary_key]
    assert pk == ["legacy_key"]
    with pytest.raises(ConnectorError, match="already exists"):           # once per table name; nothing changes
        w.load_table(server.tgt_schema, "MIG_BP_T", cols, ["LEGACY_KEY"], iter(data), mode="create")
    assert w.count(server.tgt_schema, table) == 3
    again = w.load_table(server.tgt_schema, "MIG_BP_T", cols, ["LEGACY_KEY"], iter(data[:2]), mode="replace")
    assert again.replaced_existing and w.count(server.tgt_schema, table) == 2
    w.load_table(server.tgt_schema, "MIG_BP_T", cols, ["LEGACY_KEY"], iter(data[:1]), mode="replace")   # leave a clean state for the next server run


def test_failed_loads_leave_no_table_behind(server):
    w = connector_for(server.spec("rw"))
    cols = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("V", "text", 10)]
    table = w.natural_case("MIG_FAIL_T")

    def boom():                                                            # the data source dies halfway through
        yield ("a", 1, "x")
        yield ("b", 1, "y")
        raise RuntimeError("source went away")
    with pytest.raises(ConnectorError, match="rolled back|failed"):
        w.load_table(server.tgt_schema, "MIG_FAIL_T", cols, ["LEGACY_KEY"], boom(), batch_size=1)
    assert table not in {t.name for t in w.tables(server.tgt_schema)}

    with pytest.raises(ConnectorError, match="rolled back"):               # a duplicate key violates the primary key
        w.load_table(server.tgt_schema, "MIG_FAIL_T", cols, ["LEGACY_KEY"], iter([("a", 1, "x"), ("a", 1, "dup")]))
    assert table not in {t.name for t in w.tables(server.tgt_schema)}

    with pytest.raises(ConnectorError, match="rolled back"):               # a value longer than its column
        w.load_table(server.tgt_schema, "MIG_FAIL_T", cols, ["LEGACY_KEY"], iter([("a", 1, "this value is far too long for ten")]))
    assert table not in {t.name for t in w.tables(server.tgt_schema)}

    with pytest.raises(ConnectorError, match="row count check failed"):    # the target holds fewer rows than were expected
        w.load_table(server.tgt_schema, "MIG_FAIL_T", cols, ["LEGACY_KEY"], iter([("a", 1, "x")]), expected_rows=2)
    assert table not in {t.name for t in w.tables(server.tgt_schema)}
    assert w.load_table(server.tgt_schema, "MIG_FAIL_T", cols, ["LEGACY_KEY"], iter([("a", 1, "x")])).rows_in_target == 1   # and a good load still works afterwards
    w.load_table(server.tgt_schema, "MIG_FAIL_T", cols, ["LEGACY_KEY"], iter([("a", 1, "x")]), mode="replace")


# ------------------------------------------------------------------ through the application
def _save(client, server, role):
    user, pw, db = server.ro if role == "source" else server.rw
    body = {"name": f"{server.name}_{role}", "dialect": server.dialect, "host": server.host, "port": server.port, "database": db, "user": user,
            "password": pw, "role": role, "options": {"connect_timeout": 15}}
    if role == "target":
        body["default_schema"] = server.tgt_schema
    r = client.post("/api/connections", json=body)
    assert r.status_code == 201, r.text
    assert pw not in r.text
    return body["name"]


def test_import_a_table_through_the_api(server, client):
    name = _save(client, server, "source")
    t = client.post("/api/connections/test", json={"name": name}).json()
    assert t["ok"] and t["server_version"], t
    assert server.src_schema in client.get(f"/api/connections/{name}/schemas").json()
    tables = {x["name"].lower(): x for x in client.get(f"/api/connections/{name}/tables", params={"schema_name": server.src_schema}).json()}
    assert {"customers", "order_lines", "no_key", "active_customers"} <= set(tables)
    imp = client.post("/api/sources/import-db", json={"connection": name, "schema_name": server.src_schema, "table": tables["customers"]["name"]})
    assert imp.status_code == 201, imp.text
    body = imp.json()
    assert body["rows_landed"] == 4 and body["control_status"] == "MATCH" and body["origin"]["dialect"] == server.dialect and body["origin"]["source_row_count"] == 4
    listed = {x["table_name"].lower(): x for x in client.get("/api/tables").json()}["customers"]
    assert listed["system_type"] == server.dialect.upper() if server.dialect != "mssql" else listed["system_type"] == "MSSQL"
    profile = client.post(f"/api/profile/{body['table_id']}").json()
    assert profile["total_rows"] == 4
    zips = {v["value"] for c in profile["columns"] if c["column_name"].lower() == "zip" for v in c["top_values"]}
    assert zips == {"99999"}                                                    # a postal code is personal data: the profile shows its shape only...
    import duckdb
    landed = {r[0] for r in duckdb.sql(f"SELECT zip FROM read_parquet('{client.app.state.ctx.source_file(body['table_id']).as_posix()}')").fetchall()}
    assert "02134" in landed                                                    # ...while the landed data kept the leading zero through the real database and the landing
    names = {c["column_name"].lower(): c for c in profile["columns"]}
    assert names["zip"]["null_count"] == 1 and names["notes"]["null_count"] >= 1
    pk = [c["column_name"].lower() for c in profile["columns"] if c["column_name"].lower() == "cust_id"]
    assert pk == ["cust_id"]
    composite = client.post("/api/sources/import-db", json={"connection": name, "schema_name": server.src_schema, "table": tables["order_lines"]["name"]})
    assert composite.status_code == 201 and composite.json()["rows_landed"] == 3
    assert client.post("/api/sources/import-db", json={"connection": name, "schema_name": server.src_schema, "table": tables["active_customers"]["name"]}).status_code == 201   # a view


def test_deliver_a_signed_off_run_into_the_real_database(server, client, ctx, ready_run):
    name = _save(client, server, "target")
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    ok = client.post(f"/api/connections/{name}/test-write", json={"schema_name": server.tgt_schema}).json()
    assert ok["ok"], ok
    asked = {"connection": name, "table_name": "MIG_BP_REAL"}
    pv = client.get(f"/api/pipeline/load-db/{run_id}/preview", params=asked).json()
    assert pv["rows"] == valid and "PRIMARY KEY" in pv["ddl"]
    native = {c["name"].lower(): c["type"] for c in pv["columns"]}
    print(server.name, "->", native["legacy_key"], "|", native["mig_run_id"], "|", native["but000_name_org1"])
    sign_off(client, run_id)
    done = client.post(f"/api/pipeline/load-db/{run_id}", json=asked)
    assert done.status_code == 201, done.text
    d = done.json()
    assert d["rows_loaded"] == d["rows_in_target"] == d["valid_records"] == valid and d["dialect"] == server.dialect

    w = connector_for(server.spec("rw"))
    table = w.natural_case("MIG_BP_REAL")
    assert w.count(server.tgt_schema, table) == valid                          # counted by a fresh connection, not by the loader
    import duckdb
    payload = {r[0]: r[1:] for r in duckdb.connect().execute(
        "SELECT CAST(_SRC_PK AS VARCHAR), BUT000_NAME_ORG1, ADRC_COUNTRY, KNB1_ZTERM, ADRC_POST_CODE1 FROM read_parquet(?)", [ctx.load_validation_report(run_id).valid_path]).fetchall()}
    import tempfile
    from pathlib import Path
    out = w.extract_csv(server.tgt_schema, table, None, Path(tempfile.mkdtemp()) / "t.csv.gz", 50_000_000)
    with gzip.open(out.path, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header = [h.lower() for h in rows[0]]
    idx = {h: header.index(h) for h in ("legacy_key", "but000_name_org1", "adrc_country", "knb1_zterm", "adrc_post_code1")}
    stored = {r[idx["legacy_key"]]: tuple((r[idx[c]] or None) for c in ("but000_name_org1", "adrc_country", "knb1_zterm", "adrc_post_code1")) for r in rows[1:]}
    assert stored == {k: tuple(v) for k, v in payload.items()}                  # every value identical in the real database: accents, zip leading zeros, SAP keys
    again = client.post(f"/api/pipeline/load-db/{run_id}", json=asked)
    assert again.status_code == 422 and "already exists" in again.json()["detail"]
    replaced = client.post(f"/api/pipeline/load-db/{run_id}", json={**asked, "mode": "replace"})
    assert replaced.status_code == 201 and replaced.json()["replaced_existing"]
    checks = {c["name"]: c for c in client.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}
    delivery = [c for k, c in checks.items() if k.startswith(f"db_delivery_{server.dialect}_")]
    assert len(delivery) == 1 and delivery[0]["passed"] and f"{valid} row(s) counted" in delivery[0]["detail"]
    w.load_table(server.tgt_schema, "MIG_BP_REAL", [ColumnDef("A", "int", nullable=False)], ["A"], iter([(1,)]), mode="replace")   # leave the schema tidy


# ------------------------------------------------------------------ behaviour added after the code review, proven on the real servers
def test_a_failed_replace_keeps_the_existing_table_and_a_good_replace_swaps_it(server, tmp_path):
    w = connector_for(server.spec("rw"))
    cols = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("V", "text", 10)]
    table = w.natural_case("MIG_SWAP_T")
    staging = w.natural_case(f"{table[:25]}_NEW")
    v1 = [("a", 1, "one"), ("b", 1, "two"), ("c", 1, "three")]
    w.load_table(server.tgt_schema, "MIG_SWAP_T", cols, ["LEGACY_KEY"], iter(v1), mode="replace")             # also proves replace works on a table that does not exist yet
    assert w.count(server.tgt_schema, table) == 3

    def boom():                                                                                                # the new data dies halfway through
        yield ("x", 2, "new1")
        yield ("y", 2, "new2")
        raise RuntimeError("source went away")
    with pytest.raises(ConnectorError):
        w.load_table(server.tgt_schema, "MIG_SWAP_T", cols, ["LEGACY_KEY"], boom(), mode="replace", batch_size=1)
    names = {t.name for t in w.tables(server.tgt_schema)}
    assert table in names and staging not in names                                                             # the original survives, and nothing is left over
    out = w.extract_csv(server.tgt_schema, table, None, tmp_path / "keep.csv.gz", 1_000_000)
    with gzip.open(out.path, "rt", encoding="utf-8", newline="") as fh:
        assert sorted(r[0] for r in list(csv.reader(fh))[1:]) == ["a", "b", "c"]                              # the old rows are intact, byte for byte

    with pytest.raises(ConnectorError, match="rolled back"):                                                   # a value too long for its column fails the same way
        w.load_table(server.tgt_schema, "MIG_SWAP_T", cols, ["LEGACY_KEY"], iter([("z", 3, "this is far too long")]), mode="replace")
    assert w.count(server.tgt_schema, table) == 3

    good = w.load_table(server.tgt_schema, "MIG_SWAP_T", cols, ["LEGACY_KEY"], iter([("n1", 4, "x"), ("n2", 4, "y")]), mode="replace")
    assert good.replaced_existing and w.count(server.tgt_schema, table) == 2
    names = {t.name for t in w.tables(server.tgt_schema)}
    assert table in names and staging not in names and good.ddl.count(staging) == 0
    assert [c.name.lower() for c in w.columns(server.tgt_schema, table) if c.is_primary_key] == ["legacy_key"]  # the swapped-in table keeps its primary key


def test_sql_server_reads_one_consistent_snapshot_when_the_database_allows_it(server):
    if server.name != "mssql":
        pytest.skip("SQL Server only")
    import pytds
    admin = pytds.connect(dsn=server.host, port=server.port, user="sa", password="Strong_Pw_123!", database="master", autocommit=True, login_timeout=20)
    cur = admin.cursor()
    cur.execute("ALTER DATABASE legacy_test SET ALLOW_SNAPSHOT_ISOLATION ON")
    c = connector_for(server.spec("ro"))
    with c.session() as conn:
        first = c._query(conn, "SELECT COUNT(*) FROM dbo.customers")[0][0]
        iso = c._query(conn, "SELECT transaction_isolation_level FROM sys.dm_exec_sessions WHERE session_id = @@SPID")[0][0]
        w = pytds.connect(dsn=server.host, port=server.port, user="sa", password="Strong_Pw_123!", database="legacy_test", autocommit=True, login_timeout=20)
        w.cursor().execute("INSERT INTO dbo.customers (cust_id, cust_name) VALUES (9001, 'inserted mid-read')")     # another session changes the table
        again = c._query(conn, "SELECT COUNT(*) FROM dbo.customers")[0][0]
        w.close()
    assert iso == 5 and first == again == 4                                                                      # 5 = SNAPSHOT: the count and the extract see the same table
    admin.close()


def test_awkward_values_survive_every_real_database_and_a_later_edit_is_detected(server):
    """Emoji, accents, combining marks, edge whitespace, line breaks, backslashes and wildcards, blanks: the value check must pass on the real
    products (their character sets, padding and empty-string rules differ), and a later edit of one value must be found by the comparison."""
    from src.connectors.fingerprint import Fingerprint
    w = connector_for(server.spec("rw"))
    cols = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("NAME", "text", 40), ColumnDef("NOTE", "text", 60)]
    data = [("k1", 1, "Emoji \U0001F600 B\u00e4r", "trailing  "), ("k2", 1, "  leading", "tab\there"), ("k3", 1, "line1\nline2", "C:\dir\file 100% a_b"),
            ("k4", 1, "", "\u00dcn\u00efc\u00f6d\u00e9 \ufb01 \u00df NBSP\u00a0x"), ("k5", 1, None, "e\u0301 (e plus a combining accent)"), ("k6", 1, "x" * 40, "O'Brien \"quoted\"")]
    out = w.load_table(server.tgt_schema, "MIG_VAL_T", cols, ["LEGACY_KEY"], iter(data), mode="create", expected_rows=len(data))
    assert out.values_verified and out.cells_compared == len(data) * len(cols) and len(out.value_digest) == 16
    table = w.natural_case("MIG_VAL_T")
    names = [w.natural_case(c.name) for c in cols]
    expected = Fingerprint(names, [0])
    for row in data:
        expected.add(row)
    held = w.fingerprint_table(server.tgt_schema, table, names, [names[0]])
    assert held.rows == len(data) and expected.differing_columns(held) == [] and held.digest == out.value_digest   # the same fingerprint at delivery and later

    q = w.quote                                                          # someone edits one value in the target afterwards
    with w.session(read_only=False) as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE {q(server.tgt_schema)}.{q(table)} SET {q(names[3])} = {w._placeholder(1)} WHERE {q(names[0])} = {w._placeholder(2)}", ("changed by hand", "k2"))
        cur.close()
        conn.commit()
    assert expected.differing_columns(w.fingerprint_table(server.tgt_schema, table, names, [names[0]])) == [names[3]]
    w.load_table(server.tgt_schema, "MIG_VAL_T", cols, ["LEGACY_KEY"], iter(data[:1]), mode="replace")   # leave a clean state for the next run
