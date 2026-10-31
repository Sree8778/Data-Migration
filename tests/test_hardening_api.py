"""Regression tests (API level) for failures found by stress-testing: a database `replace` must never drop a table this application did not
create, and several uploads of the same table at once must all succeed."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from tests.test_db_targets import pg_query, pg_target, ready_run, save_target, sign_off  # noqa: F401
import time

from tests.test_module7_api import SCRIPT, api_env, client, csv_path, ctx, dataset, encoder, env, subset, upload  # noqa: F401


def test_replace_refuses_a_table_this_application_did_not_create(client, pg_target, ready_run):
    p, conn = pg_target
    assert save_target(client, p).status_code == 201
    sign_off(client, ready_run["run_id"])
    cur = conn.cursor()
    cur.execute("CREATE TABLE mig_target.important_data (id int primary key, secret text)")
    cur.execute("INSERT INTO mig_target.important_data SELECT g, 'keep me' FROM generate_series(1, 50) g")

    body = {"connection": "ora_like_target", "table_name": "important_data", "mode": "replace"}
    refused = client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json=body)
    assert refused.status_code == 422 and "was not created by a delivery of this application" in refused.json()["detail"], refused.text
    assert pg_query(conn, "SELECT count(*), min(secret) FROM mig_target.important_data")[0] == (50, "keep me")   # untouched

    # a table an earlier delivery created is still replaceable
    mine = {"connection": "ora_like_target", "table_name": "mig_bp_run"}
    assert client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json=mine).status_code == 201
    again = client.post(f"/api/pipeline/load-db/{ready_run['run_id']}", json={**mine, "mode": "replace"})
    assert again.status_code == 201 and again.json()["replaced_existing"], again.text



def test_concurrent_uploads_of_the_same_table_do_not_fail(client, dataset):
    rows = subset(dataset, 40, clean=True)
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda _: upload(client, rows).status_code, range(6)))
    assert codes == [201] * 6, codes


def _wait(client, job_id, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/pipeline/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.5)
    raise AssertionError("the job did not finish")


def test_a_pipeline_run_can_be_queued_and_polled(client, api_env):
    mid = api_env["mapping"].id
    queued = client.post(f"/api/pipeline/run/{mid}/background", json={"wave_name": "BG"})
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["kind"] == "pipeline" and job["status"] in ("queued", "running", "succeeded")
    done = _wait(client, job["job_id"])
    assert done["status"] == "succeeded", done
    result = done["result"]
    assert result["run_id"] > 0 and result["validation"]["input_records"] > 0 and "readiness" in result
    assert client.get(f"/api/runs/{result['run_id']}").json()["status"] == "SUCCEEDED"
    assert done["progress"]["message"]   # the last stage is shown

    # the cheap refusals come back at once, not as a failed job later
    assert client.post("/api/pipeline/run/999999/background", json={}).status_code == 404
    assert client.get("/api/pipeline/jobs/nope").status_code == 404


# ------------------------------------------------------------------ a delivery the server did not live to finish
def _simulate_crash_after_commit(ctx, run_id, record):
    """What a kill between the target's COMMIT and the delivery record looks like: the table is there, the record is not, the marker is."""
    run_dir = ctx.run_dir(run_id)
    (run_dir / "db_loads.json").unlink()
    ctx.db_loader._set_pending(run_dir, [{
        "run_id": run_id, "connection": record["connection"], "dialect": record["dialect"], "host": record["host"], "port": record["port"], "database": record["database"],
        "schema": record["schema"] if "schema" in record else record["schema_name"], "table": record["table"], "mode": "create", "columns": record["columns"],
        "include_source_columns": False, "valid_records": record["valid_records"], "started_by": "alice", "started_at": record["loaded_at"]}])


def test_an_interrupted_delivery_is_noticed_and_can_be_adopted_only_if_the_table_matches(client, ctx, pg_target, ready_run):
    p, conn = pg_target
    run_id = ready_run["run_id"]
    assert save_target(client, p).status_code == 201
    sign_off(client, run_id)
    body = {"connection": "ora_like_target", "table_name": "mig_crash"}
    delivered = client.post(f"/api/pipeline/load-db/{run_id}", json=body)
    assert delivered.status_code == 201, delivered.text
    assert client.get(f"/api/pipeline/load-db/{run_id}/pending").json() == []          # a finished delivery leaves no marker

    _simulate_crash_after_commit(ctx, run_id, delivered.json())
    pending = client.get(f"/api/pipeline/load-db/{run_id}/pending").json()
    assert len(pending) == 1 and pending[0]["table"] == "mig_crash" and pending[0]["valid_records"] == ready_run["valid"]
    blocked = client.post(f"/api/pipeline/load-db/{run_id}", json={**body, "mode": "replace"})
    assert blocked.status_code == 422 and "did not finish" in blocked.json()["detail"], blocked.text

    # the table is damaged as well: adopting it must be refused, and the marker stays
    cur = conn.cursor()
    cur.execute("DELETE FROM mig_target.mig_crash WHERE legacy_key IN (SELECT legacy_key FROM mig_target.mig_crash LIMIT 3)")
    refused = client.post(f"/api/pipeline/recover-db/{run_id}", json={**body, "action": "adopt"})
    assert refused.status_code == 422 and "does not hold exactly" in refused.json()["detail"], refused.text
    assert len(client.get(f"/api/pipeline/load-db/{run_id}/pending").json()) == 1

    # discard forgets the marker; the (own) table can be replaced by a fresh delivery
    assert client.post(f"/api/pipeline/recover-db/{run_id}", json={**body, "action": "discard"}).json() == {"action": "discarded", "delivery": None}
    assert client.get(f"/api/pipeline/load-db/{run_id}/pending").json() == []
    again = client.post(f"/api/pipeline/load-db/{run_id}", json={**body, "mode": "replace"})
    assert again.status_code == 201 and again.json()["rows_in_target"] == ready_run["valid"], again.text

    # the intact table of a second crash IS adopted, and the delivery is recorded as recovered, verified
    _simulate_crash_after_commit(ctx, run_id, again.json())
    adopted = client.post(f"/api/pipeline/recover-db/{run_id}", json={**body, "action": "adopt"})
    assert adopted.status_code == 200, adopted.text
    delivery = adopted.json()["delivery"]
    assert adopted.json()["action"] == "adopted" and delivery["recovered_after_interruption"] and delivery["values_verified"]
    assert delivery["rows_in_target"] == ready_run["valid"] and delivery["verifications"][0]["verified"]
    assert client.get(f"/api/pipeline/load-db/{run_id}/pending").json() == []
    assert [d["table"] for d in client.get(f"/api/pipeline/load-db/{run_id}").json()] == ["mig_crash"]
    assert client.post(f"/api/pipeline/recover-db/{run_id}", json={**body, "action": "adopt"}).status_code == 422       # nothing left to settle


def test_a_refused_delivery_leaves_no_marker_behind(client, pg_target, ready_run):
    p, _ = pg_target
    run_id = ready_run["run_id"]
    assert save_target(client, p).status_code == 201
    sign_off(client, run_id)
    body = {"connection": "ora_like_target", "table_name": "mig_twice"}
    assert client.post(f"/api/pipeline/load-db/{run_id}", json=body).status_code == 201
    assert client.post(f"/api/pipeline/load-db/{run_id}", json=body).status_code == 422          # 'create' onto an existing table is refused
    assert client.get(f"/api/pipeline/load-db/{run_id}/pending").json() == []


def test_require_auth_refuses_to_start_without_api_keys(ctx):
    import pytest
    from dataclasses import replace
    from src.api.main import create_app
    from src.api.settings import Settings
    assert Settings.from_env({"MIGRATION_REQUIRE_AUTH": "true"}).require_auth and not Settings.from_env({}).require_auth
    ctx.settings = replace(ctx.settings, require_auth=True, api_keys={})
    with pytest.raises(RuntimeError, match="refusing to start"):
        create_app(ctx)
    ctx.settings = replace(ctx.settings, api_keys={"k1": "alice"})
    assert create_app(ctx) is not None
