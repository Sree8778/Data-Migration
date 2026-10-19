"""Delta analysis: after the source changes, what is new, changed, unchanged or gone since an earlier run."""

from __future__ import annotations

import duckdb
import pytest

from tests.test_db_targets import ready_run  # noqa: F401
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401


def parquet_rows(path, key="_SRC_PK"):
    return duckdb.connect().execute(f"SELECT * FROM read_parquet('{path}') ORDER BY {key}").fetchall()


@pytest.fixture()
def two_runs(client, ctx, ready_run):
    """Run A is the ready run. The landed source is then edited (5 customers move city, 3 disappear, 2 get an unknown country, 4 are new) and run B follows."""
    client_, mapping = client, ready_run["mapping"]
    table_id = client_.get(f"/api/mapping/{mapping}").json()["mapping"]["source_table_id"]
    path = ctx.source_file(table_id).as_posix()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE src AS SELECT * FROM read_parquet('{path}')")
    ids = [r[0] for r in con.execute("SELECT CUST_ID FROM src ORDER BY CAST(CUST_ID AS BIGINT)").fetchall()]
    valid_a = {r[0] for r in duckdb.connect().execute(f"SELECT CAST(_SRC_PK AS VARCHAR) FROM read_parquet('{ctx.load_validation_report(ready_run['run_id']).valid_path}')").fetchall()}
    # only edit customers that were valid in run A, so every expectation below is exact
    pick = [i for i in ids if i in valid_a]
    moved, gone, bad = pick[0:5], pick[5:8], pick[8:10]
    lit = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
    con.execute(f"UPDATE src SET CITY = 'Changedville' WHERE CUST_ID IN ({lit(moved)})")
    con.execute(f"DELETE FROM src WHERE CUST_ID IN ({lit(gone)})")
    con.execute(f"UPDATE src SET COUNTRY = 'Atlantis' WHERE CUST_ID IN ({lit(bad)})")
    for n, (name, street, city) in enumerate([("Zyxwv Quokka GmbH", "Hafenstrasse 1", "Kiel"), ("Plimth Orchards Ltd", "Orchard Lane 2", "Leeds"),
                                              ("Vandelay Imports SA", "Rue Neuve 3", "Lyon"), ("Trombone Logistics BV", "Havenweg 4", "Utrecht")]):
        con.execute(f"INSERT INTO src SELECT * REPLACE ('{9000001 + n}' AS CUST_ID, '{name}' AS CUST_NAME, '' AS TAX_ID, '' AS EMAIL, "
                    f"'{street}' AS ADDR_STREET, '{city}' AS CITY) FROM src WHERE CUST_ID = '{pick[20]}'")
    con.execute(f"COPY src TO '{path}' (FORMAT parquet)")
    run_b = client_.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "DELTA_B"})
    assert run_b.status_code == 201, run_b.text
    return {"a": ready_run["run_id"], "b": run_b.json()["run_id"], "moved": moved, "gone": gone, "bad": bad, "valid_a": ready_run["valid"],
            "valid_b": run_b.json()["validation"]["valid_records"], "mapping": mapping}


def test_a_delta_names_what_is_new_changed_unchanged_and_gone_and_why(client, ctx, two_runs):
    a, b = two_runs["a"], two_runs["b"]
    r = client.post(f"/api/runs/{b}/delta", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["baseline_run_id"] == a and d["run_id"] == b and d["columns_compared"] >= 6
    c = d["counts"]
    assert (c["new"], c["changed"], c["removed"]) == (4, 5, 5)                     # 4 added, 5 moved city, 3 deleted + 2 now rejected
    assert c["current_valid"] == two_runs["valid_b"] and c["baseline_valid"] == two_runs["valid_a"]
    assert c["unchanged"] == two_runs["valid_a"] - 5 - 5                           # nothing else differs
    assert c["new"] + c["changed"] + c["unchanged"] == c["current_valid"] and c["removed"] + c["changed"] + c["unchanged"] == c["baseline_valid"]
    assert d["removed_reasons"] == {"NOW_REJECTED": 2, "NOT_IN_CURRENT_VALID": 3}
    assert set(d["examples"]["REMOVED"]) <= set(two_runs["gone"] + two_runs["bad"])

    delivered = parquet_rows(d["delta_path"])
    assert len(delivered) == 9
    cols = duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_parquet('{d['delta_path']}')").fetchall()
    assert "_DELTA_KIND" in [x[0] for x in cols]
    kinds = duckdb.connect().execute(f"SELECT _DELTA_KIND, count(*) FROM read_parquet('{d['delta_path']}') GROUP BY 1 ORDER BY 1").fetchall()
    assert kinds == [("CHANGED", 5), ("NEW", 4)]
    keys = {str(x[0]) for x in duckdb.connect().execute(f"SELECT _SRC_PK FROM read_parquet('{d['delta_path']}')").fetchall()}
    assert set(two_runs["moved"]) <= keys and {"9000001", "9000004"} <= keys          # the moved customers and the new ones, no others
    why = dict(duckdb.connect().execute(f"SELECT CAST(_SRC_PK AS VARCHAR), _WHY FROM read_parquet('{d['removed_path']}')").fetchall())
    assert {k: v for k, v in why.items() if k in two_runs["bad"]} == {k: "NOW_REJECTED" for k in two_runs["bad"]}
    assert {k: v for k, v in why.items() if k in two_runs["gone"]} == {k: "NOT_IN_CURRENT_VALID" for k in two_runs["gone"]}
    assert client.get(f"/api/runs/{b}/delta").json()["counts"] == c                  # kept with the run


def test_the_baseline_defaults_to_the_latest_earlier_run_and_can_be_chosen(client, ctx, two_runs):
    a, b = two_runs["a"], two_runs["b"]
    assert client.post(f"/api/runs/{b}/delta", json={"baseline_run": a}).json()["baseline_run_id"] == a
    third = client.post(f"/api/pipeline/run/{two_runs['mapping']}", json={"wave_name": "DELTA_C"}).json()["run_id"]
    same = client.post(f"/api/runs/{third}/delta", json={}).json()
    assert same["baseline_run_id"] == b and same["counts"]["new"] == same["counts"]["changed"] == same["counts"]["removed"] == 0    # nothing changed since run B
    assert same["counts"]["unchanged"] == same["counts"]["current_valid"] and same["delta_path"] is None and same["removed_path"] is None
    against_a = client.post(f"/api/runs/{third}/delta", json={"baseline_run": a}).json()
    assert against_a["baseline_run_id"] == a and against_a["counts"]["new"] == 4


def test_a_delta_needs_an_earlier_validated_run_with_the_same_target_fields(client, ctx, two_runs):
    a, b = two_runs["a"], two_runs["b"]
    assert client.post(f"/api/runs/{a}/delta", json={}).status_code == 409                                  # nothing earlier to compare with
    assert client.post(f"/api/runs/{a}/delta", json={"baseline_run": b}).status_code == 422                 # the baseline must be earlier
    assert client.post(f"/api/runs/{b}/delta", json={"baseline_run": 9999}).status_code == 422                # a baseline that is not earlier, whether or not it exists
    assert client.post("/api/runs/9999/delta", json={}).status_code == 404
    # a different mapping (fewer target fields) cannot be compared: it would be a full delivery
    table_id = client.get(f"/api/mapping/{two_runs['mapping']}").json()["mapping"]["source_table_id"]
    small = client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
    client.post(f"/api/mapping/{small}/rules", json={"target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "DIRECT_COPY", "source_column": "CUST_NAME"})
    run_small = client.post(f"/api/pipeline/run/{small}", json={"deduplicate": False}).json()["run_id"]
    different = client.post(f"/api/runs/{run_small}/delta", json={"baseline_run": a})
    assert different.status_code == 422 and "mapping changed" in different.json()["detail"]
    assert client.get(f"/api/runs/{a}/delta").json() is None
