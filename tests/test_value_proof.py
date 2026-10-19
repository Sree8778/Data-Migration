"""Value-level proof for database deliveries: the values in the target are the values that were sent, not just the same number of rows.

A content fingerprint (every cell hashed with its row key, summed per column) is compared before the commit, so a delivery whose values were
altered on the way in is refused; the same comparison can be repeated later against the live table to catch anything changed afterwards.
"""

from __future__ import annotations

import pytest

from src.connectors.dialects import PostgresConnector
from src.connectors.fingerprint import Fingerprint
from tests.test_db_targets import pg_query, pg_target, ready_run, save_target, sign_off  # noqa: F401
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401


# ------------------------------------------------------------------ the fingerprint itself
def fp(rows, columns=("KEY", "NAME", "CITY")):
    f = Fingerprint(list(columns), [0])
    for r in rows:
        f.add(r)
    return f


ROWS = [("1", "Müller GmbH", "Köln"), ("2", "Acme", None), ("3", "Beta", "Rom")]


def test_the_fingerprint_ignores_row_order_and_the_null_versus_empty_difference_but_nothing_else():
    base = fp(ROWS)
    assert fp(list(reversed(ROWS))).differing_columns(base) == [] and fp(list(reversed(ROWS))).digest == base.digest
    assert fp([("1", "Müller GmbH", "Köln"), ("2", "Acme", ""), ("3", "Beta", "Rom")]).differing_columns(base) == []  # Oracle stores '' as NULL
    assert fp([("1", "Müller GmbH", "Köln"), ("2", "Acme", None), (3, "Beta", "Rom")]).differing_columns(base) == []  # an int that prints the same is the same


def test_every_kind_of_alteration_is_named_by_column():
    base = fp(ROWS)
    assert fp([("1", "M?ller GmbH", "Köln"), ROWS[1], ROWS[2]]).differing_columns(base) == ["NAME"]           # an umlaut lost on the way in
    assert fp([("1", "Müller Gmb", "Köln"), ROWS[1], ROWS[2]]).differing_columns(base) == ["NAME"]            # a value cut short
    assert fp([("1", "Müller GmbH", "Köln"), ("2", "Acme", " "), ROWS[2]]).differing_columns(base) == ["CITY"]  # a space where there was nothing
    swapped = fp([("1", "Acme", "Köln"), ("2", "Müller GmbH", None), ROWS[2]])                                   # two names swapped between rows
    assert swapped.differing_columns(base) == ["NAME"] and swapped.digest != base.digest
    assert fp(ROWS[:2]).differing_columns(base) == ["KEY", "NAME", "CITY"] and fp(ROWS + [("4", "Extra", "Wien")]).differing_columns(base)  # row counts differ
    with pytest.raises(ValueError):
        fp(ROWS, columns=("A", "B", "C")).differing_columns(base)
    with pytest.raises(ValueError):
        base.add(("only", "two"))


def test_a_value_mixed_up_between_two_rows_is_caught_even_though_the_totals_match():
    a = fp([("1", "X", "p"), ("2", "Y", "q")])
    b = fp([("1", "Y", "p"), ("2", "X", "q")])  # the same two names exist in the column, on the wrong rows
    assert a.differing_columns(b) == ["NAME"]


# ------------------------------------------------------------------ deliveries into a real PostgreSQL
def deliver(client, run_id, table, **extra):
    return client.post(f"/api/pipeline/load-db/{run_id}", json={"connection": "ora_like_target", "table_name": table, **extra})


def certificate_checks(client, run_id):
    return {c["name"]: c for c in client.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}


def test_a_delivery_is_value_checked_before_the_commit_and_the_certificate_says_so(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    save_target(client, p)
    sign_off(client, run_id)
    done = deliver(client, run_id, "MIG_BP_VAL")
    assert done.status_code == 201, done.text
    d = done.json()
    columns = pg_query(conn, "SELECT count(*) FROM information_schema.columns WHERE table_schema='mig_target' AND table_name='mig_bp_val'")[0][0]
    assert d["values_verified"] is True and d["cells_compared"] == valid * columns and len(d["value_digest"]) == 16
    check = certificate_checks(client, run_id)["db_values_postgresql_mig_target.mig_bp_val"]
    assert check["passed"] and f"{valid * columns:,} cell(s) read back" in check["detail"] and d["value_digest"] in check["detail"]


def test_altered_values_are_refused_and_nothing_is_kept(client, ctx, pg_target, ready_run, monkeypatch):
    p, conn = pg_target
    save_target(client, p)
    sign_off(client, ready_run["run_id"])
    real = PostgresConnector._insert_many

    def lossy(self, cur, sql, batch):  # a database whose character set cannot hold what was sent: the right rows arrive with wrong values
        real(self, cur, sql, [tuple(("?" + v[1:]) if i == 2 and isinstance(v, str) and v else v for i, v in enumerate(row)) for row in batch])
    monkeypatch.setattr(PostgresConnector, "_insert_many", lossy)
    r = deliver(client, ready_run["run_id"], "MIG_BP_BAD")
    assert r.status_code == 422 and "value check failed" in r.json()["detail"] and "nothing was kept" in r.json()["detail"], r.text
    assert pg_query(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0   # table and rows gone
    assert not (ctx.run_dir(ready_run["run_id"]) / "db_loads.json").exists()                              # and no record of a delivery that did not happen
    monkeypatch.setattr(PostgresConnector, "_insert_many", real)
    assert deliver(client, ready_run["run_id"], "MIG_BP_BAD").status_code == 201                            # the same request works once the database is right


def test_switching_the_value_check_off_is_allowed_but_the_certificate_does_not_pass_it(client, ctx, pg_target, ready_run):
    p, _ = pg_target
    save_target(client, p)
    sign_off(client, ready_run["run_id"])
    d = deliver(client, ready_run["run_id"], "MIG_BP_NOCHECK", verify_values=False).json()
    assert d["values_verified"] is False and d["cells_compared"] == 0
    check = certificate_checks(client, ready_run["run_id"])["db_values_postgresql_mig_target.mig_bp_nocheck"]
    assert check["passed"] is False and "NOT compared" in check["detail"]


def test_a_table_changed_after_delivery_is_found_by_the_on_demand_comparison(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    save_target(client, p)
    sign_off(client, run_id)
    assert deliver(client, run_id, "MIG_BP_LIVE").status_code == 201
    ask = {"connection": "ora_like_target", "table_name": "MIG_BP_LIVE"}

    clean = client.post(f"/api/pipeline/verify-db/{run_id}", json=ask)
    assert clean.status_code == 200, clean.text
    assert clean.json()["verified"] is True and clean.json()["differing_columns"] == [] and clean.json()["rows_in_target"] == valid
    assert certificate_checks(client, run_id)["db_values_reverified_postgresql_mig_target.mig_bp_live"]["passed"]

    first = pg_query(conn, "SELECT legacy_key FROM mig_target.mig_bp_live ORDER BY 1 LIMIT 1")[0][0]
    cur = conn.cursor()
    cur.execute("UPDATE mig_target.mig_bp_live SET but000_name_org1 = 'Edited by hand' WHERE legacy_key = %s", (first,))   # someone fixes a name in the target
    edited = client.post(f"/api/pipeline/verify-db/{run_id}", json=ask).json()
    assert edited["verified"] is False and edited["differing_columns"] == ["but000_name_org1"] and edited["rows_in_target"] == valid
    check = certificate_checks(client, run_id)["db_values_reverified_postgresql_mig_target.mig_bp_live"]
    assert check["passed"] is False and "DIFFERENT in column(s) but000_name_org1" in check["detail"]

    cur.execute("DELETE FROM mig_target.mig_bp_live WHERE legacy_key = %s", (first,))                                      # and then a row disappears
    gone = client.post(f"/api/pipeline/verify-db/{run_id}", json=ask).json()
    assert gone["verified"] is False and gone["rows_in_target"] == valid - 1 and gone["rows_in_payload"] == valid and len(gone["differing_columns"]) > 1
    history = client.get(f"/api/pipeline/load-db/{run_id}").json()[0]["verifications"]
    assert [v["verified"] for v in history] == [True, False, False] and history[0]["checked_by"] == "alice"                 # every comparison is kept


def test_verifying_something_that_was_never_delivered_is_refused(client, ctx, pg_target, ready_run):
    p, _ = pg_target
    save_target(client, p)
    r = client.post(f"/api/pipeline/verify-db/{ready_run['run_id']}", json={"connection": "ora_like_target", "table_name": "NEVER_DELIVERED"})
    assert r.status_code == 422 and "no recorded delivery" in r.json()["detail"]
    assert client.post(f"/api/pipeline/verify-db/{ready_run['run_id']}", json={"connection": "ghost", "table_name": "X"}).status_code == 404
