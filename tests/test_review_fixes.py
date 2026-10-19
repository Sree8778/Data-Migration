"""Regression tests for the problems an independent code review found in the connectors, each one reproduced first and then fixed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

from src.connectors.base import ColumnDef, ConnectionSpec, ConnectorError
from src.connectors.dialects import MssqlConnector, MysqlConnector, OracleConnector, PostgresConnector
from src.connectors.storage import AzureBlobConnector, S3Connector, SftpConnector
from src.connectors.store import ConnectionInputError, ConnectionStore
from src.services.db_loader_service import DbLoaderService
from tests.test_db_targets import COLS, ROWS, FakeTargetConn, connector, pg_target, save_target  # noqa: F401
from tests.test_module7_api import client, ctx, dataset, encoder, env, make_ctx  # noqa: F401


# ------------------------------------------------------------------ a fake source connection that can misbehave
class Cur:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        self.conn.executed.append(sql)
        if "snapshot_isolation_state" in sql:
            self._rows = [(0,)]
            return
        if "COUNT(*)" in sql:
            self._rows = [(len(self.conn.data),)]
        elif "all_tab_columns" in sql or "INFORMATION_SCHEMA.COLUMNS" in sql or "information_schema.columns" in sql or "pg_attribute" in sql:
            self._rows = [("A", "text", None, 1, 0, 1), ("B", "text", None, 1, 0, 2)]
        elif "all_tables" in sql or "sys.tables" in sql or "information_schema.tables" in sql or "pg_class" in sql:
            self._rows = [("T1", "TABLE", 1)]
        elif self.conn.fail_select and " FROM " in sql:
            raise RuntimeError(self.conn.fail_select)          # the data SELECT itself is refused
        else:
            self.description = [("A",), ("B",)]
            self._rows = list(self.conn.data)

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def close(self):
        pass


class SrcConn:
    def __init__(self, data=(), fail_select=None):
        self.data, self.fail_select = list(data), fail_select
        self.executed, self.rollbacks, self.closed = [], 0, False

    def cursor(self, *a, **k):
        return Cur(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def set_session(self, **kwargs):          # psycopg2's read-only switch
        self.executed.append(f"set_session {kwargs}")


def mk(cls, conn, **opts):
    return cls(ConnectionSpec(cls.dialect, "h", cls.default_port, "db", "u", "top-secret-pw", opts), connect=lambda: conn)


# ------------------------------------------------------------------ 1. a storage connection stays inside the container it was given
def test_a_connection_fixed_to_one_container_cannot_read_another():
    s3 = S3Connector(ConnectionSpec("s3", "h", 443, "only-bucket", "k", "s", {}))
    assert s3._check_container("only-bucket") == "only-bucket"
    for other in ("other-bucket", "only-bucket2", "/", "etc"):
        with pytest.raises(ConnectorError, match="limited to bucket 'only-bucket'"):
            s3._check_container(other)
    free = S3Connector(ConnectionSpec("s3", "h", 443, "", "k", "s", {}))                      # no default bucket: browsing every bucket is the point
    assert free._check_container("any-bucket") == "any-bucket"
    az = AzureBlobConnector(ConnectionSpec("azure_blob", "h", 443, "pinned", "acct", "key", {}))
    with pytest.raises(ConnectorError, match="limited to container 'pinned'"):
        az._check_container("other")
    sftp = SftpConnector(ConnectionSpec("sftp", "h", 22, "/data/outbound", "u", "p", {}))
    assert sftp._check_container("/data/outbound") == "/data/outbound"
    for other in ("/", "/etc", "/data", "/data/outbound/../.."):
        with pytest.raises(ConnectorError):
            sftp._check_container(other)
    assert SftpConnector(ConnectionSpec("sftp", "h", 22, "", "u", "p", {}))._check_container("/") == "/"      # no base directory: the login's root is the boundary


# ------------------------------------------------------------------ 2. driver errors on the SELECT are connector errors, and never leak the password
def test_a_refused_select_or_read_only_request_becomes_a_clear_error(tmp_path):
    for cls in (OracleConnector, MysqlConnector, MssqlConnector, PostgresConnector):
        c = mk(cls, SrcConn(data=[("1", "x")], fail_select="permission denied for table T1 (top-secret-pw)"))
        with pytest.raises(ConnectorError, match="could not run the extract") as e:
            c.extract_csv("S", "T1", None, tmp_path / "x.csv.gz", 1_000_000)
        assert "top-secret-pw" not in str(e.value) and not (tmp_path / "x.csv.gz").exists()
        with pytest.raises(ConnectorError, match="could not run the extract"):
            mk(cls, SrcConn(data=[("1", "x")], fail_select="timeout")).preview("S", "T1")

    class Refuses(SrcConn):
        def cursor(self, *a, **k):
            raise RuntimeError("SET TRANSACTION not allowed here")
    mysql = mk(MysqlConnector, Refuses())
    with pytest.raises(ConnectorError, match="would not start a read-only session"):
        mysql.count("S", "T1")


# ------------------------------------------------------------------ 3. the size cap holds for a few huge rows too
def test_the_size_cap_counts_bytes_not_just_rows(tmp_path):
    blob = os.urandom(700_000).hex()                                    # incompressible: ~1.4 MB of text per row
    few_huge_rows = [("1", blob), ("2", blob), ("3", blob)]
    c = mk(PostgresConnector, SrcConn(data=few_huge_rows))
    target = tmp_path / "huge.csv.gz"
    with pytest.raises(ConnectorError, match="larger than the 2 MB limit"):
        c.extract_csv("S", "T1", None, target, max_bytes=2 * 1024 * 1024)
    assert not target.exists()
    one_row_over = mk(PostgresConnector, SrcConn(data=[("1", blob)]))     # under the 8 MB check interval: only the final check can catch it
    with pytest.raises(ConnectorError, match="larger than the"):
        one_row_over.extract_csv("S", "T1", None, tmp_path / "one.csv.gz", max_bytes=200_000)
    assert not (tmp_path / "one.csv.gz").exists()
    ok = mk(PostgresConnector, SrcConn(data=[("1", "small")])).extract_csv("S", "T1", None, tmp_path / "ok.csv.gz", max_bytes=1_000_000)
    assert ok.rows == 1


def test_mysql_discards_a_half_read_stream_instead_of_reading_it_to_the_end():
    conn = SrcConn(data=[("1", "x")])
    mk(MysqlConnector, conn)._end_session(conn)
    assert conn.closed and conn.rollbacks == 0                           # ROLLBACK would make the driver read the rest of an unbuffered result first
    other = SrcConn()
    mk(PostgresConnector, other)._end_session(other)
    assert other.closed and other.rollbacks == 1                          # everywhere else a rollback releases locks before closing


# ------------------------------------------------------------------ 4. replace never costs the table that was already there
@pytest.mark.parametrize("cls,schema", [(OracleConnector, "APP"), (MysqlConnector, "app")])
def test_replace_on_products_that_cannot_roll_back_ddl_keeps_the_old_table_until_the_new_one_is_verified(cls, schema):
    probe = connector(cls, FakeTargetConn())
    old = probe.natural_case("MIG_T")
    staging = probe.natural_case(f"{old[:25]}_NEW")
    # a failed replace: the new table is built under a temporary name and dropped again; the original is never touched
    failing = FakeTargetConn(schemas=(schema,), tables=[old], fail_inserts=True)
    with pytest.raises(ConnectorError, match="rolled back"):
        connector(cls, failing).load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS), mode="replace")
    drops = [s for s in failing.executed if s.startswith("DROP TABLE")]
    assert drops and all(staging in d for d in drops) and not any(f'"{old}"' in d or f"`{old}`" in d for d in drops)
    assert any(f"CREATE TABLE" in s and staging in s for s in failing.executed)
    # a successful replace: verified first, then the old table is dropped and the new one renamed into place
    good = FakeTargetConn(schemas=(schema,), tables=[old])
    out = connector(cls, good).load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS), mode="replace", expected_rows=2)
    order = [s.split(" (")[0].split("\n")[0] for s in good.executed if s.startswith(("CREATE TABLE", "DROP TABLE", "ALTER TABLE", "RENAME TABLE"))]
    assert out.replaced_existing and out.rows_inserted == 2
    assert order[0].startswith("CREATE TABLE") and staging in order[0] and order[1].startswith("DROP TABLE") and old in order[1] and ("RENAME" in order[2])
    assert out.ddl.count(old) >= 1 and staging not in out.ddl                 # the recorded DDL is the final table's, not the temporary one
    # a leftover from an interrupted replace is reported, not overwritten
    leftover = FakeTargetConn(schemas=(schema,), tables=[old, staging])
    with pytest.raises(ConnectorError, match="left over from an interrupted replace"):
        connector(cls, leftover).load_table(schema, "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS), mode="replace")
    assert not any(s.startswith(("DROP TABLE", "CREATE TABLE")) for s in leftover.executed)


def test_replace_on_transactional_ddl_stays_one_transaction():
    conn = FakeTargetConn(schemas=("dbo",), tables=["MIG_T"], fail_inserts=True)
    c = connector(MssqlConnector, conn)
    with pytest.raises(ConnectorError, match="rolled back"):
        c.load_table("dbo", "MIG_T", COLS, ["LEGACY_KEY"], iter(ROWS), mode="replace")
    assert conn.rollbacks >= 1 and conn.commits == 0                       # the DROP and CREATE are undone by the rollback, nothing was committed


def test_two_columns_that_collide_after_case_folding_are_refused_before_any_ddl():
    cols = [ColumnDef("LEGACY_KEY", "text", 20, False), ColumnDef("SRC_Name", "text", 5), ColumnDef("SRC_NAME", "text", 5)]
    conn = FakeTargetConn(schemas=("APP",))
    with pytest.raises(ConnectorError, match="same name"):
        connector(OracleConnector, conn).load_table("APP", "MIG_T", cols, ["LEGACY_KEY"], iter([]))
    assert not any(s.startswith("CREATE") for s in conn.executed)


# ------------------------------------------------------------------ 5. passwords belong to the server they were typed for
def test_password_handling_is_strict(tmp_path):
    store = ConnectionStore(tmp_path, env={"DB_PW": "from-env"})
    base = dict(name="c1", dialect="postgresql", host="db1.example.com", port=5432, database="d", user="u")
    store.save(base, "alice", password="typed-pw")
    assert store.spec_for("c1", password_override="").password == "typed-pw"                 # an empty override is "none given"
    assert store.spec_for("c1", password_override="fresh").password == "fresh"
    store.save({**base, "database": "other_db"}, "alice")                                      # an unrelated edit keeps the password
    assert store.spec_for("c1").password == "typed-pw"
    store.save({**base, "host": "attacker.example.com"}, "mallory")                            # repointing the connection forgets it
    with pytest.raises(ConnectorError, match="no password is available"):
        store.spec_for("c1")
    store.save({**base, "host": "attacker.example.com"}, "alice", password="new-pw")           # unless the caller supplies the password for the new server
    assert store.spec_for("c1").password == "new-pw"
    store.save({**base, "user": "someone_else"}, "mallory")
    with pytest.raises(ConnectorError):
        store.spec_for("c1")
    assert store.resolve_password("typed", "DB_PW") == "typed" and store.resolve_password(None, "DB_PW") == "from-env" and store.resolve_password(None, None) == ""
    with pytest.raises(ConnectionInputError, match="not set"):
        store.resolve_password(None, "NOPE_NOT_SET")
    with pytest.raises(ConnectionInputError, match="environment variable name"):
        store.resolve_password(None, "bad name!")


def test_a_connection_can_be_tested_with_only_a_password_variable(client, engine, monkeypatch):
    url = engine.url
    monkeypatch.setenv("MIG_TEST_DB_PASSWORD", url.password or "")
    body = {"dialect": "postgresql", "host": url.host or "localhost", "port": url.port or 5432, "database": url.database, "user": url.username}
    ok = client.post("/api/connections/test", json={**body, "password_env": "MIG_TEST_DB_PASSWORD"}).json()
    assert ok["ok"], ok
    missing = client.post("/api/connections/test", json={**body, "password_env": "MIG_TEST_DB_NOT_SET"})
    assert missing.status_code == 422 and "not set on the server" in missing.json()["detail"]
    assert client.post("/api/connections/test", json={**body, "password": "wrong"}).json()["ok"] is False


# ------------------------------------------------------------------ 6. delivery with real legacy column names
def test_legacy_column_names_become_valid_unique_short_names():
    safe = DbLoaderService.safe_column_name
    taken: set[str] = {"legacy_key", "mig_run_id"}
    names = []
    for raw in ("SRC_Customer Name", "SRC_customer name", "SRC_Tax-ID", "SRC_CUSTOMER_ADDRESS_LINE_1_LONGER_THAN_THIRTY", "SRC_CUSTOMER_ADDRESS_LINE_1_LONGER_THAN_FORTY", "SRC_$$$", "SRC_Ünïcode Näme"):
        n = safe(raw, taken)
        taken.add(n.lower())
        names.append(n)
    assert names[0] == "SRC_Customer_Name" and names[1] == "SRC_customer_name_2"                  # collide case-insensitively: second one gets a suffix
    assert names[2] == "SRC_Tax_ID" and all(len(n) <= 30 for n in names) and len({n.lower() for n in names}) == len(names)
    assert names[3] != names[4] and names[3].startswith("SRC_CUSTOMER_ADDRESS_LINE_1") and names[4].endswith("_2")
    from src.connectors.base import SAFE_NAME
    assert all(SAFE_NAME.match(n) for n in names), names


def test_a_delivery_with_awkward_source_columns_succeeds(pg_target, tmp_path):
    p, conn = pg_target
    parquet = tmp_path / "valid.parquet"
    con = duckdb.connect()
    con.execute("""COPY (SELECT '1001' AS _SRC_PK, 'Müller GmbH' AS BUT000_NAME_ORG1, 'Customer One' AS "SRC_Customer Name", 'c1' AS "SRC_customer name",
                         'T-1' AS "SRC_Tax-ID", 'long' AS SRC_CUSTOMER_ADDRESS_LINE_1_LONGER_THAN_THIRTY
                    UNION ALL SELECT '1002', 'Café', NULL, NULL, NULL, 'x') TO '%s' (FORMAT parquet)""" % parquet.as_posix())

    class Svc(DbLoaderService):
        def _target_lengths(self):
            return {"BUT000_NAME_ORG1": 40}
    spec = ConnectionSpec("postgresql", p["host"], p["port"], p["database"], p["user"], p["password"], {})
    record = Svc(None).deliver(PostgresConnector(spec), 5, parquet, tmp_path / "run", "c", "mig_target", "MIG_AWKWARD", "create", "alice", include_source_columns=True)
    assert record["rows_loaded"] == record["rows_in_target"] == 2
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='mig_target' AND table_name='mig_awkward' ORDER BY ordinal_position")
    cols = [r[0] for r in cur.fetchall()]
    assert cols[:3] == ["legacy_key", "mig_run_id", "but000_name_org1"] and len(cols) == len(set(cols)) == 7 and all(len(c) <= 30 for c in cols)
    assert "src_tax_id" in cols and "src_customer_name" in cols
    cur.execute("SELECT * FROM mig_target.mig_awkward WHERE legacy_key = '1001'")
    values = set(cur.fetchone())
    assert {"Customer One", "c1", "T-1", "long"} <= values                       # every awkward column arrived with its own value


# ------------------------------------------------------------------ 7. one damaged snapshot does not break the table list
def test_a_damaged_snapshot_file_does_not_break_the_table_list(client, ctx):
    r = client.post("/api/sources/register", data={"system_name": "S", "table_name": "T_OK"}, files={"file": ("a.csv", b"A,B\n1,2\n")})
    assert r.status_code == 201
    bad = client.post("/api/sources/register", data={"system_name": "S", "table_name": "T_BAD"}, files={"file": ("b.csv", b"A,B\n3,4\n")}).json()
    path = ctx.table_dir(bad["table_id"]) / "snapshot.json"
    good_text = path.read_text(encoding="utf-8")
    path.write_text("{ this is not json", encoding="utf-8")
    listed = {t["table_name"]: t for t in client.get("/api/tables").json()}
    assert set(listed) == {"T_OK", "T_BAD"} and listed["T_OK"]["source_format"] == "delimited" and listed["T_BAD"]["source_format"] is None
    assert client.get(f"/api/tables/{bad['table_id']}/snapshot").status_code == 404                  # reported as "no landed snapshot", not a crash
    path.write_text(good_text, encoding="utf-8")                                                       # repairing the file is picked up (the cache is keyed on the file's mtime and size)
    assert {t["table_name"]: t for t in client.get("/api/tables").json()}["T_BAD"]["source_format"] == "delimited"
