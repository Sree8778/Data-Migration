"""Delta deliveries into a database target: only new and changed records are written to the table an earlier run delivered, every replaced or deleted row
is kept so the merge can be undone exactly, and the certificate checks the arithmetic and the values."""

from __future__ import annotations

import duckdb
import pytest

from tests.test_db_targets import pg_query, pg_target, ready_run, save_target, sign_off  # noqa: F401
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

TABLE = "MIG_BP_MERGE"
SCHEMA_TABLE = "mig_target.mig_bp_merge"


@pytest.fixture()
def changed_source(client, ctx, ready_run):
    """Run A is the ready run. The landed source is then edited: 5 customers move city, 3 disappear, 4 are new. Run B follows (all of it valid)."""
    mapping = ready_run["mapping"]
    table_id = client.get(f"/api/mapping/{mapping}").json()["mapping"]["source_table_id"]
    path = ctx.source_file(table_id).as_posix()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE src AS SELECT * FROM read_parquet('{path}')")
    valid_a = {r[0] for r in con.execute(f"SELECT CAST(_SRC_PK AS VARCHAR) FROM read_parquet('{ctx.load_validation_report(ready_run['run_id']).valid_path}')").fetchall()}
    pick = [r[0] for r in con.execute("SELECT CUST_ID FROM src ORDER BY CAST(CUST_ID AS BIGINT)").fetchall() if r[0] in valid_a]
    moved, gone = pick[0:5], pick[5:8]
    lit = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
    con.execute(f"UPDATE src SET CITY = 'Changedville' WHERE CUST_ID IN ({lit(moved)})")
    con.execute(f"DELETE FROM src WHERE CUST_ID IN ({lit(gone)})")
    for n, (name, street, city) in enumerate([("Zyxwv Quokka GmbH", "Hafenstrasse 1", "Kiel"), ("Plimth Orchards Ltd", "Orchard Lane 2", "Leeds"),
                                              ("Vandelay Imports SA", "Rue Neuve 3", "Lyon"), ("Trombone Logistics BV", "Havenweg 4", "Utrecht")]):
        con.execute(f"INSERT INTO src SELECT * REPLACE ('{9000001 + n}' AS CUST_ID, '{name}' AS CUST_NAME, '' AS TAX_ID, '' AS EMAIL, "
                    f"'{street}' AS ADDR_STREET, '{city}' AS CITY) FROM src WHERE CUST_ID = '{pick[20]}'")
    con.execute(f"COPY src TO '{path}' (FORMAT parquet)")
    run_b = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "DELTA_B"})
    assert run_b.status_code == 201, run_b.text
    assert run_b.json()["readiness"]["state"] == "READY_FOR_LOAD", run_b.json()["readiness"]
    return {"a": ready_run["run_id"], "b": run_b.json()["run_id"], "valid_a": ready_run["valid"], "moved": moved, "gone": gone, "mapping": mapping}


def deliver(client, run_id, **extra):
    return client.post(f"/api/pipeline/load-db/{run_id}", json={"connection": "ora_like_target", "table_name": TABLE, **extra})


def merge(client, run_id, baseline, **extra):
    return deliver(client, run_id, mode="merge", baseline_run=baseline, **extra)


def dump(conn):
    return [tuple(r) for r in pg_query(conn, f"SELECT * FROM {SCHEMA_TABLE} ORDER BY legacy_key")]


def prepared(client, p, s):
    """A delivers into the table; B is signed off, ready to merge."""
    save_target(client, p)
    sign_off(client, s["a"])
    assert deliver(client, s["a"]).status_code == 201
    sign_off(client, s["b"])


def test_only_the_delta_is_written_into_the_table_the_baseline_delivered(client, ctx, pg_target, changed_source):
    p, conn = pg_target
    s = changed_source
    prepared(client, p, s)
    before = dump(conn)
    assert len(before) == s["valid_a"]

    done = merge(client, s["b"], s["a"])
    assert done.status_code == 201, done.text
    d = done.json()
    assert (d["inserted_new"], d["updated"], d["deleted"], d["mode"], d["baseline_run"]) == (4, 5, 0, "merge", s["a"])
    assert d["rows_loaded"] == d["valid_records"] == 9 and d["rows_before"] == s["valid_a"] and d["rows_in_target"] == s["valid_a"] + 4
    assert d["before_rows"] == 5 and d["values_verified"] is True and d["cells_compared"] > 0

    after = dump(conn)
    assert len(after) == s["valid_a"] + 4                                                    # nothing was deleted: the 3 who left the source are still there
    old, new = {r[0]: r for r in before}, {r[0]: r for r in after}
    untouched = [k for k in old if k not in s["moved"]]
    assert all(old[k] == new[k] for k in untouched)                                           # every other row is byte-for-byte what run A delivered
    cols = [c[0] for c in pg_query(conn, "SELECT column_name FROM information_schema.columns WHERE table_schema='mig_target' AND table_name='mig_bp_merge' ORDER BY ordinal_position")]
    city = cols.index("adrc_city1")
    assert all(new[k][city] == "Changedville" and old[k][city] != "Changedville" for k in s["moved"])
    run_ids = {k: new[k][cols.index("mig_run_id")] for k in new}
    assert {run_ids[k] for k in s["moved"]} == {s["b"]} and run_ids["9000001"] == s["b"] and {v for k, v in run_ids.items() if k in untouched} == {s["a"]}

    c = {x["name"]: x for x in client.get(f"/api/reconciliation/{s['b']}").json()["report"]["checks"]}
    merge_check = c["db_merge_postgresql_mig_target.mig_bp_merge"]
    assert merge_check["passed"] and "4 new, 5 replaced, 0 deleted" in merge_check["detail"] and not any(n.startswith("db_delivery_") for n in c)
    assert c["db_values_postgresql_mig_target.mig_bp_merge"]["passed"]


def test_removed_records_are_deleted_only_when_asked_and_a_rollback_restores_the_table_exactly(client, ctx, pg_target, changed_source):
    p, conn = pg_target
    s = changed_source
    prepared(client, p, s)
    original = dump(conn)

    done = merge(client, s["b"], s["a"], delete_removed=True)
    assert done.status_code == 201, done.text
    d = done.json()
    assert (d["inserted_new"], d["updated"], d["deleted"]) == (4, 5, 3) and d["before_rows"] == 8 and d["rows_in_target"] == s["valid_a"] + 4 - 3
    assert {r[0] for r in pg_query(conn, f"SELECT legacy_key FROM {SCHEMA_TABLE}")}.isdisjoint(s["gone"])

    undone = client.post(f"/api/pipeline/rollback-db/{s['b']}", json={"connection": "ora_like_target", "table_name": TABLE, "reason": "the delta was wrong"})
    assert undone.status_code == 200, undone.text
    u = undone.json()
    assert (u["rows_removed"], u["rows_restored"], u["table_dropped"], u["rows_remaining"]) == (9, 8, False, s["valid_a"])
    assert dump(conn) == original                                                              # the table is exactly what run A left, deleted customers included
    c = {x["name"]: x for x in client.get(f"/api/reconciliation/{s['b']}").json()["report"]["checks"]}
    assert c["db_rollback_postgresql_mig_target.mig_bp_merge"]["passed"] and not any(n.startswith("db_merge_") for n in c)
    assert client.post(f"/api/pipeline/rollback-db/{s['b']}", json={"connection": "ora_like_target", "table_name": TABLE, "reason": "again"}).status_code == 422


def test_a_merge_that_fails_changes_nothing_and_leaves_no_record(client, ctx, pg_target, changed_source, monkeypatch):
    from src.connectors.dialects import PostgresConnector
    p, conn = pg_target
    s = changed_source
    prepared(client, p, s)
    original = dump(conn)
    real = PostgresConnector._insert_many
    calls = {"n": 0}

    def flaky(self, cur, sql, batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection lost mid-merge")
        real(self, cur, sql, batch)
    monkeypatch.setattr(PostgresConnector, "_insert_many", flaky)
    failed = merge(client, s["b"], s["a"])
    assert failed.status_code == 422 and "rolled back" in failed.json()["detail"]
    assert dump(conn) == original and not (ctx.run_dir(s["b"]) / "db_loads.json").exists()
    assert not list(ctx.run_dir(s["b"]).glob("merge_before_*"))                                # the half-written before-image is removed too
    monkeypatch.setattr(PostgresConnector, "_insert_many", real)

    def lossy(self, cur, sql, batch):                                                          # altered values are caught by the value check
        real(self, cur, sql, [tuple(("?" + v[1:]) if i == 2 and isinstance(v, str) and v else v for i, v in enumerate(r)) for r in batch])
    monkeypatch.setattr(PostgresConnector, "_insert_many", lossy)
    bad = merge(client, s["b"], s["a"])
    assert bad.status_code == 422 and "value check failed" in bad.json()["detail"] and dump(conn) == original
    monkeypatch.setattr(PostgresConnector, "_insert_many", real)
    assert merge(client, s["b"], s["a"]).status_code == 201                                    # and it works once the database is right


def test_a_merge_needs_a_baseline_that_delivered_this_table_and_the_usual_gate(client, ctx, pg_target, changed_source):
    p, conn = pg_target
    s = changed_source
    save_target(client, p)
    sign_off(client, s["a"])
    unsigned = merge(client, s["b"], s["a"])
    assert unsigned.status_code == 409 and "not been signed off" in unsigned.json()["detail"]          # the same gate as every load
    sign_off(client, s["b"])
    assert merge(client, s["b"], None).status_code == 422                                      # a baseline is required (the body check)
    never = merge(client, s["b"], s["a"])
    assert never.status_code == 409 and "nothing to merge into" in never.json()["detail"]      # run A has not delivered anything yet
    assert deliver(client, s["a"]).status_code == 201
    assert merge(client, s["b"], s["a"], include_source_columns=True).status_code == 422        # the table has no raw legacy columns
    assert merge(client, s["b"], s["a"], delete_removed=False, table_name="OTHER_TABLE").status_code == 409
    rolled = client.post(f"/api/pipeline/rollback-db/{s['a']}", json={"connection": "ora_like_target", "table_name": TABLE, "reason": "start over"})
    assert rolled.status_code == 200
    assert merge(client, s["b"], s["a"]).status_code == 409                                    # a rolled-back delivery is nothing to merge into


def test_a_table_changed_after_the_merge_is_not_rolled_back_automatically(client, ctx, pg_target, changed_source):
    p, conn = pg_target
    s = changed_source
    prepared(client, p, s)
    assert merge(client, s["b"], s["a"]).status_code == 201
    after = dump(conn)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {SCHEMA_TABLE} WHERE legacy_key = '9000001'")                   # someone removes one of the merged-in rows by hand
    refused = client.post(f"/api/pipeline/rollback-db/{s['b']}", json={"connection": "ora_like_target", "table_name": TABLE, "reason": "undo"})
    assert refused.status_code == 422 and "changed since" in refused.json()["detail"]
    assert len(dump(conn)) == len(after) - 1                                                   # nothing was touched by the refused rollback


def test_an_empty_delta_has_nothing_to_deliver(client, ctx, pg_target, changed_source):
    p, conn = pg_target
    s = changed_source
    prepared(client, p, s)
    assert merge(client, s["b"], s["a"]).status_code == 201
    third = client.post(f"/api/pipeline/run/{s['mapping']}", json={"wave_name": "NOTHING_NEW"}).json()["run_id"]
    sign_off(client, third)
    nothing = merge(client, third, s["b"])
    assert nothing.status_code == 422 and "nothing to deliver" in nothing.json()["detail"]
