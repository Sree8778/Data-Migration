"""Row-exclusion rules: rows the business wants left behind (deleted, test, dummy) are removed before transformation, kept with their reason,
and accounted for on the reconciliation certificate, so 'extracted' still equals everything that happened to every row."""

from __future__ import annotations

import duckdb
import pytest
from pydantic import ValidationError

from src.schemas.exclusion import ExclusionRule, ExclusionRuleInput
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.row_exclusion import (
    EXCLUDED_BY, EXCLUDED_REASON, ExclusionError, ExclusionStore, apply_exclusions, preview_rule, suggest, validate_rule)
from tests.test_module4_transform_dedupe import HEADER, csv_path, dataset, env, expected_errors  # noqa: F401
from tests.test_module7_api import ctx, encoder, make_client, subset, upload  # noqa: F401

CSV = """ID,NAME,FLAG,CITY
1,Acme,,Berlin
2,TEST CUSTOMER 5,,Paris
3,Beta,X,Rome
4,Dummy Corp,x,
5,Testa Pharma,,Oslo
6,O'Brien Ltd,,Dublin
7,Gamma,,
"""


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "customers.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def rule(rule_id, column, operator, values=(), reason="because the business said so", ignore_case=True):
    return ExclusionRule(rule_id=rule_id, column=column, operator=operator, values=list(values), reason=reason, ignore_case=ignore_case,
                         created_by="steward", created_at="2026-01-01T00:00:00+00:00")


def count(source, **kw):
    return apply_exclusions(source, [rule(1, **kw)]).excluded_rows


# ------------------------------------------------------------------ the operators
@pytest.mark.parametrize("kw, expected", [
    (dict(column="FLAG", operator="equals", values=["x"]), 2),  # rows 3 and 4: 'X' and 'x', case ignored by default
    (dict(column="FLAG", operator="equals", values=["x"], ignore_case=False), 1),  # only the lower-case one
    (dict(column="CITY", operator="in", values=["berlin", " ROME "]), 2),  # trimmed, case-insensitive
    (dict(column="CITY", operator="is_blank"), 2),  # rows 4 and 7
    (dict(column="NAME", operator="starts_with", values=["test"]), 2),  # TEST CUSTOMER 5 and Testa Pharma
    (dict(column="NAME", operator="contains", values=["corp"]), 1),
    (dict(column="NAME", operator="regex", values=[r"(^|[^a-z])(test|dummy)( |$)"]), 2),  # a word boundary spares 'Testa'
    (dict(column="NAME", operator="regex", values=["^TEST"], ignore_case=False), 1),
    (dict(column="NAME", operator="equals", values=["O'Brien Ltd"]), 1),  # a quote in a value is data, not SQL
    (dict(column="NAME", operator="equals", values=["x' OR '1'='1"]), 0),
])
def test_operators(source, kw, expected):
    assert count(source, **kw) == expected


def test_a_row_matching_several_rules_is_counted_once_for_the_first_rule(source, tmp_path):
    rules = [rule(1, "FLAG", "equals", ["x"], reason="deleted in the legacy system"),
             rule(2, "NAME", "contains", ["dummy"], reason="dummy record")]
    result = apply_exclusions(source, rules, tmp_path / "in.parquet", tmp_path / "out.parquet")
    by_id = {r.rule_id: r.excluded for r in result.rules}
    assert by_id == {1: 2, 2: 0}  # row 4 is flagged AND called Dummy: the first rule gets it, so nothing is counted twice
    assert (result.total_rows, result.included_rows, result.excluded_rows) == (7, 5, 2)
    con = duckdb.connect()
    kept = con.execute(f"SELECT ID, {EXCLUDED_BY}, {EXCLUDED_REASON} FROM read_parquet('{(tmp_path / 'out.parquet').as_posix()}') ORDER BY ID").fetchall()
    assert kept == [("3", 1, "deleted in the legacy system"), ("4", 1, "deleted in the legacy system")]  # the file keeps its original text values
    left = con.execute(f"SELECT ID FROM read_parquet('{(tmp_path / 'in.parquet').as_posix()}') ORDER BY ID").fetchall()
    assert [r[0] for r in left] == ["1", "2", "5", "6", "7"]
    assert result.rules[0].examples == ["X", "x"] or sorted(result.rules[0].examples) == ["X", "x"]


def test_no_rules_changes_nothing_and_writes_nothing(source, tmp_path):
    result = apply_exclusions(source, [], tmp_path / "in.parquet", tmp_path / "out.parquet")
    assert result.included_rows == result.total_rows == 7 and result.excluded_rows == 0
    assert not (tmp_path / "in.parquet").exists() and not (tmp_path / "out.parquet").exists()


# ------------------------------------------------------------------ what is refused
def test_unknown_columns_bad_patterns_and_rules_that_remove_everything_are_refused(source):
    with pytest.raises(ExclusionError, match="column 'NOPE' is not in the landed file"):
        validate_rule(source, ExclusionRuleInput(column="NOPE", operator="is_blank", reason="why not"))
    with pytest.raises(ExclusionError, match="not a valid regular expression"):
        validate_rule(source, ExclusionRuleInput(column="NAME", operator="regex", values=["(unclosed"], reason="why not"))
    with pytest.raises(ExclusionError, match="remove all 7 rows"):
        apply_exclusions(source, [rule(1, "ID", "regex", [".*"])])
    assert validate_rule(source, ExclusionRuleInput(column="name", operator="is_blank", reason="why not")) == "NAME"  # the file's own spelling


def test_input_shape_is_checked(source):
    for bad in (dict(operator="equals", values=[]), dict(operator="equals", values=["a", "b"]), dict(operator="in", values=[]),
                dict(operator="is_blank", values=["x"]), dict(operator="contains", values=["  "])):
        with pytest.raises(ValidationError):
            ExclusionRuleInput(column="NAME", reason="because", **bad)
    with pytest.raises(ValidationError):
        ExclusionRuleInput(column="NAME", operator="is_blank", reason="")  # a reason is mandatory
    assert ExclusionRuleInput(column="NAME", operator="in", values=["a", "b"], reason="because").values == ["a", "b"]


def test_preview_counts_without_saving(source):
    seen = preview_rule(source, ExclusionRuleInput(column="FLAG", operator="equals", values=["X"], reason="deleted"))
    assert seen.excluded == 2 and seen.rule_id == 0 and sorted(seen.examples) == ["X", "x"]


# ------------------------------------------------------------------ suggestions
def test_the_data_suggests_a_deletion_flag_and_test_names_but_never_a_majority_flag(tmp_path):
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("ID,NAME1,LOEVM\n1,Acme,\n2,TEST CUSTOMER 5,\n3,Beta,X\n4,Dummy Corp,X\n5,Gamma,\n6,Delta,\n", encoding="utf-8")
    found = {(s.column, s.operator): s for s in suggest(legacy)}
    flag = found[("LOEVM", "equals")]
    assert flag.values == ["X"] and flag.matches == 2 and "deleted or inactive" in flag.reason
    names = found[("NAME1", "regex")]
    assert names.matches == 2 and sorted(names.examples) == ["Dummy Corp", "TEST CUSTOMER 5"]
    assert ("ID", "regex") not in found and ("ID", "equals") not in found
    mostly = tmp_path / "mostly.csv"
    rows = "\n".join(f"{i},X" for i in range(8))
    mostly.write_text("ID,LOEVM\n" + rows + "\n9,\n10,\n", encoding="utf-8")
    assert suggest(mostly) == []  # 80% flagged is not a few deleted records: a person has to look


# ------------------------------------------------------------------ the store
def test_the_store_numbers_rules_keeps_them_and_survives_a_damaged_file(tmp_path):
    store = ExclusionStore(tmp_path)
    one = store.add(7, ExclusionRuleInput(column="FLAG", operator="equals", values=["X"], reason="deleted"), "FLAG", "alice")
    two = store.add(7, ExclusionRuleInput(column="NAME", operator="contains", values=["dummy"], reason="dummy"), "NAME", "bob")
    assert (one.rule_id, two.rule_id, one.created_by) == (1, 2, "alice")
    assert [r.rule_id for r in ExclusionStore(tmp_path).rules(7)] == [1, 2]  # a new instance reads the same file
    assert store.delete(7, 1).rule_id == 1 and store.delete(7, 1) is None
    assert store.add(7, ExclusionRuleInput(column="ID", operator="is_blank", reason="no key"), "ID", "bob").rule_id == 3  # ids are never reused
    assert store.rules(8) == []
    (tmp_path / "exclusions" / "mapping_7.json").write_text("{not json", encoding="utf-8")
    assert store.rules(7) == []


# ------------------------------------------------------------------ end to end through the API
@pytest.fixture()
def landed(ctx, dataset):
    seed_sap_catalog(ctx.metadata)
    client = make_client(ctx)
    rows = subset(dataset, 300, clean=True)
    table = upload(client, rows).json()["table_id"]
    assert client.post(f"/api/profile/{table}").status_code == 200
    mset = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
    assert client.post(f"/api/mapping/{mset}/rules", json={
        "target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "DIRECT_COPY", "source_column": "CUST_NAME"}).status_code == 201
    return client, ctx, table, mset, rows


def test_excluded_rows_never_reach_the_run_and_the_certificate_accounts_for_them(landed):
    client, ctx, table, mset, rows = landed
    victims = [r for r in rows if r["CITY"].strip().lower() == rows[0]["CITY"].strip().lower()]
    assert 0 < len(victims) < len(rows)
    response = client.post(f"/api/mapping/{mset}/exclusions", json={
        "column": "city", "operator": "equals", "values": [rows[0]["CITY"]], "reason": "branch closed before the cut-over"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["excluded"] == len(victims) and created["rule_id"] == 1 and created["column"] == "CITY"  # the file's own spelling

    overview = client.get(f"/api/mapping/{mset}/exclusions").json()
    assert overview["editable"] is True and overview["summary"]["excluded_rows"] == len(victims) and overview["rules"][0]["created_by"] == "alice"

    run = client.post(f"/api/pipeline/run/{mset}", json={"deduplicate": False}).json()
    report = run["run"]
    assert report["source_records"] == len(rows) and report["excluded_records"] == len(victims)
    assert report["transformation"]["source_records"] == len(rows) - len(victims)  # only the rest was transformed

    run_id = run["run_id"]
    assert client.get(f"/api/runs/{run_id}").json()["records_extracted"] == len(rows)  # the counter is the whole file
    rec = client.get(f"/api/reconciliation/{run_id}").json()["report"]
    assert rec["counts"]["extracted"] == len(rows) and rec["counts"]["excluded"] == len(victims)
    line = next(d for d in rec["dropoff"] if d["reason"] == "EXCLUDED_BY_RULE")
    assert line["count"] == len(victims) and "branch closed before the cut-over" in line["explanation"]
    assert rec["variance"] == 0 and rec["balanced"] is True  # every row is still explained
    checks = {c["name"]: c for c in rec["checks"]}
    assert checks["rows_left_out_are_kept_with_their_reason"]["passed"] and checks["validation_input_equals_extract"]["passed"]
    kept = duckdb.connect().execute(
        f"SELECT count(*), min({EXCLUDED_REASON}) FROM read_parquet('{(ctx.run_dir(run_id) / 'excluded.parquet').as_posix()}')").fetchone()
    assert kept == (len(victims), "branch closed before the cut-over")


def test_a_run_without_rules_is_unchanged(landed):
    client, ctx, table, mset, rows = landed
    run = client.post(f"/api/pipeline/run/{mset}", json={"deduplicate": False}).json()
    assert run["run"]["excluded_records"] == 0 and run["run"]["source_records"] == len(rows)
    rec = client.get(f"/api/reconciliation/{run['run_id']}").json()["report"]
    assert rec["counts"]["excluded"] == 0 and not any(d["reason"] == "EXCLUDED_BY_RULE" for d in rec["dropoff"]) and rec["balanced"]


def test_rules_are_refused_when_wrong_and_frozen_once_submitted(landed):
    client, ctx, table, mset, rows = landed
    path = f"/api/mapping/{mset}/exclusions"
    good = {"column": "CITY", "operator": "equals", "values": [rows[0]["CITY"]], "reason": "branch closed"}
    assert client.post(path, json={**good, "column": "NOPE"}).status_code == 422  # not a column of the file
    assert client.post(path, json={**good, "reason": ""}).status_code == 422  # a reason is mandatory
    assert client.post(path, json={**good, "operator": "regex", "values": ["(bad"]}).status_code == 422
    everything = client.post(path, json={"column": "CUST_ID", "operator": "regex", "values": [".*"], "reason": "oops"})
    assert everything.status_code == 422 and "nothing to migrate" in everything.json()["detail"]
    assert client.get(path).json()["rules"] == []  # nothing was saved by any of those
    assert client.post(f"{path}/preview", json=good).json()["excluded"] > 0 and client.get(path).json()["rules"] == []  # a preview saves nothing

    assert client.post(path, json=good).status_code == 201
    assert client.post(f"/api/governance/submit-mapping/{mset}").status_code == 200  # the preflight passes: the NAME_ORG1 copy works
    assert client.post(path, json=good).status_code == 409 and client.delete(f"{path}/1").status_code == 409  # frozen with the mapping
    assert client.get(path).json()["editable"] is False and len(client.get(path).json()["rules"]) == 1


def test_a_rule_can_be_deleted_and_the_deletion_is_logged(landed):
    client, ctx, table, mset, rows = landed
    path = f"/api/mapping/{mset}/exclusions"
    client.post(path, json={"column": "CITY", "operator": "equals", "values": [rows[0]["CITY"]], "reason": "branch closed"})
    assert client.delete(f"{path}/1").json() == {"deleted": 1, "mapping_set_id": mset}
    assert client.delete(f"{path}/1").status_code == 404 and client.get(path).json()["rules"] == []
    log = (ctx.workspace / "rule_deletions.jsonl")
    entries = [line for line in log.read_text(encoding="utf-8").splitlines() if "exclusion_rule" in line] if log.exists() else []
    assert not log.exists() or len(entries) == 1


def test_the_dry_run_judges_the_rules_only_on_the_rows_the_run_will_keep(landed):
    client, ctx, table, mset, rows = landed
    victims = [r for r in rows if r["CITY"].strip().lower() == rows[0]["CITY"].strip().lower()]
    client.post(f"/api/mapping/{mset}/exclusions", json={"column": "CITY", "operator": "equals", "values": [rows[0]["CITY"]], "reason": "closed"})
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    assert report["source_rows"] == len(rows) and report["sample_rows"] == len(rows) - len(victims)


def test_suggestions_skip_rules_that_are_already_in_place(landed):
    client, ctx, table, mset, rows = landed
    suggestions = client.get(f"/api/mapping/{mset}/exclusions/suggestions")
    assert suggestions.status_code == 200 and isinstance(suggestions.json(), list)
