"""Rolling back a database delivery: exactly the run's rows are removed, all-or-nothing, with a reason, and the certificate says so."""

from __future__ import annotations

from tests.test_db_targets import pg_query, pg_target, ready_run, save_target, sign_off  # noqa: F401
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401


def deliver(client, run_id, table, **extra):
    return client.post(f"/api/pipeline/load-db/{run_id}", json={"connection": "ora_like_target", "table_name": table, **extra})


def rollback(client, run_id, table, reason="the business asked for a re-run", **extra):
    return client.post(f"/api/pipeline/rollback-db/{run_id}", json={"connection": "ora_like_target", "table_name": table, "reason": reason, **extra})


def checks(client, run_id):
    return {c["name"]: c for c in client.get(f"/api/reconciliation/{run_id}").json()["report"]["checks"]}


def test_a_delivery_is_rolled_back_the_table_goes_with_it_and_the_certificate_says_so(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    save_target(client, p)
    sign_off(client, run_id)
    assert deliver(client, run_id, "MIG_BP_RB").status_code == 201
    assert pg_query(conn, "SELECT count(*) FROM mig_target.mig_bp_rb")[0][0] == valid

    done = rollback(client, run_id, "MIG_BP_RB")
    assert done.status_code == 200, done.text
    r = done.json()
    assert (r["rows_removed"], r["rows_remaining"], r["table_dropped"], r["by"], r["reason"]) == (valid, 0, True, "alice", "the business asked for a re-run")
    assert pg_query(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mig_target'")[0][0] == 0   # nothing left behind

    listed = client.get(f"/api/pipeline/load-db/{run_id}").json()[0]
    assert listed["rolled_back"]["rows_removed"] == valid
    c = checks(client, run_id)
    assert c["db_rollback_postgresql_mig_target.mig_bp_rb"]["passed"] and "rolled back by alice" in c["db_rollback_postgresql_mig_target.mig_bp_rb"]["detail"]
    assert not any(n.startswith("db_delivery_") for n in c)                       # a delivery that was undone is no longer expected to be in the table

    again = rollback(client, run_id, "MIG_BP_RB")
    assert again.status_code == 422 and "already rolled back" in again.json()["detail"]
    assert deliver(client, run_id, "MIG_BP_RB").status_code == 201                  # the name is free again: deliver once more
    assert checks(client, run_id)["db_delivery_postgresql_mig_target.mig_bp_rb"]["passed"]


def test_only_this_runs_rows_go_and_the_table_stays_when_others_are_in_it(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    save_target(client, p)
    sign_off(client, run_id)
    assert deliver(client, run_id, "MIG_BP_MIX").status_code == 201
    cur = conn.cursor()
    cur.execute("INSERT INTO mig_target.mig_bp_mix (legacy_key, mig_run_id) VALUES ('FROM_ANOTHER_RUN', 999)")      # a row some other run delivered here
    r = rollback(client, run_id, "MIG_BP_MIX").json()
    assert (r["rows_removed"], r["rows_remaining"], r["table_dropped"]) == (valid, 1, False)
    assert pg_query(conn, "SELECT legacy_key, mig_run_id FROM mig_target.mig_bp_mix") == [("FROM_ANOTHER_RUN", 999)]


def test_a_table_that_was_changed_since_the_delivery_is_not_rolled_back_automatically(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id, valid = ready_run["run_id"], ready_run["valid"]
    save_target(client, p)
    sign_off(client, run_id)
    deliver(client, run_id, "MIG_BP_EDIT")
    cur = conn.cursor()
    cur.execute("DELETE FROM mig_target.mig_bp_edit WHERE legacy_key = (SELECT min(legacy_key) FROM mig_target.mig_bp_edit)")
    refused = rollback(client, run_id, "MIG_BP_EDIT")
    assert refused.status_code == 422 and "changed since" in refused.json()["detail"], refused.text
    assert pg_query(conn, "SELECT count(*) FROM mig_target.mig_bp_edit")[0][0] == valid - 1                       # nothing was touched
    assert client.get(f"/api/pipeline/load-db/{run_id}").json()[0]["rolled_back"] is None


def test_a_reason_is_required_and_unknown_things_are_refused(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id = ready_run["run_id"]
    save_target(client, p)
    sign_off(client, run_id)
    deliver(client, run_id, "MIG_BP_REASON")
    assert rollback(client, run_id, "MIG_BP_REASON", reason="").status_code == 422
    assert rollback(client, run_id, "MIG_BP_REASON", reason="ab").status_code == 422
    assert rollback(client, run_id, "NEVER_DELIVERED").status_code == 422
    assert client.post(f"/api/pipeline/rollback-db/{run_id}", json={"connection": "ghost", "table_name": "X", "reason": "because"}).status_code == 404
    assert pg_query(conn, "SELECT count(*) FROM mig_target.mig_bp_reason")[0][0] == ready_run["valid"]            # the refused attempts changed nothing
