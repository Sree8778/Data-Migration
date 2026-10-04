"""Module 5 tests: pre-load validation, defect aggregation, Jira sync, governance gate.

The end-to-end fixtures reuse the Module 4 dirty customer dataset and pipeline (imported
fixtures), so validation runs on real transformed + deduplicated staging output.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import duckdb
import httpx
import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from src.db.enums import MappingStatus, MigrationPhase, RunStatus
from src.db.models import GovDefectAggregate, MapFieldRule, MapTableRule, MigMigrationRun
from src.schemas.validation import (
    FailureKind,
    GateState,
    Severity,
    kind_for_signature,
    severity_for_signature,
    signature_for,
)
from src.services.deduplication_engine import DeduplicationEngine
from src.services.defect_service import DefectService
from src.services.domain_provider import (
    ChainedDomainProvider,
    CsvDomainProvider,
    SeedDomainProvider,
)
from src.services.execution_orchestrator import ExecutionOrchestrator
from src.services.governance_service import (
    GateBlockedError,
    GovernanceService,
    InvalidTransitionError,
    NotAuthorizedError,
    SegregationOfDutiesError,
)
from src.services.jira_service import JiraAuthError, JiraConfig, JiraService
from src.services.metadata_service import NotFoundError
from src.services.transformation_engine import TransformationEngine
from src.services.validation_engine import ValidationEngine
from tests.test_module4_transform_dedupe import (  # noqa: F401  (fixtures + helpers from Module 4)
    DEDUP,
    PASSTHROUGH,
    COUNTRY_SEED,
    PAYMENT_TERMS_SEED,
    csv_path,
    dataset,
    env,
    read_parquet,
)

STAGING_COLS = ["BUT000_BP_EXT", "BUT000_NAME_ORG1", "BUT000_BU_SORT1", "BUT000_BU_GROUP", "BUT000_TYPE",
                "ADRC_COUNTRY", "ADRC_CITY1", "ADRC_POST_CODE1", "ADRC_STREET", "KNB1_ZTERM"]
GOOD = dict(BUT000_BP_EXT="0000001001", BUT000_NAME_ORG1="Acme", BUT000_BU_SORT1="ACME", BUT000_BU_GROUP="BP01",
            BUT000_TYPE="2", ADRC_COUNTRY="US", ADRC_CITY1="Boston", ADRC_POST_CODE1="02134",
            ADRC_STREET="1 MAIN ST", KNB1_ZTERM="NT30")


# ------------------------------------------------------------------ helpers
def write_staging(path, rows: list[dict]):
    """Hand-built Module 4-shaped staging: rows are dicts (pk, overrides, optional errors / dup flag)."""
    frame = pd.DataFrame([{
        "_SRC_PK": r["pk"], **{**GOOD, **r.get("set", {})}, "_TRANSFORM_STATUS": "OK",
        "_TRANSFORM_ERRORS": r.get("errors"), "is_duplicate_child": r.get("dup", False),
    } for r in rows])
    con = duckdb.connect()
    con.register("frame", frame)
    con.execute(f"COPY (SELECT * REPLACE (CAST(_TRANSFORM_ERRORS AS VARCHAR) AS _TRANSFORM_ERRORS) FROM frame) "
                f"TO '{path.as_posix()}' (FORMAT parquet)")
    return path


def make_run(session_factory, mapping_id, status=RunStatus.SUCCEEDED) -> int:
    with session_factory.begin() as s:
        run = MigMigrationRun(mapping_set_id=mapping_id, wave_name="W", current_phase=MigrationPhase.TRANSFORM,
                              status=status)
        s.add(run)
        s.flush()
        return run.id


def defect_rows(session_factory, run_id) -> dict[str, GovDefectAggregate]:
    with session_factory() as s:
        return {d.rule_signature: d for d in s.scalars(
            sa.select(GovDefectAggregate).where(GovDefectAggregate.run_id == run_id))}


@pytest.fixture()
def pipeline(env, session_factory, csv_path, tmp_path):
    run = ExecutionOrchestrator(session_factory, TransformationEngine(session_factory), DeduplicationEngine(),
                                output_dir=tmp_path / "out").execute_run(
        env["mapping"].id, csv_path, "WAVE_1", DEDUP, passthrough_columns=PASSTHROUGH)
    report = ValidationEngine(session_factory).validate(
        env["mapping"].id, run.deduplicated_path, tmp_path / "validation", run_id=run.run_id)
    return run, report, env


def expected_signatures(dataset, child_pks) -> Counter:
    """Independent model of the failures among the non-duplicate rows."""
    exp: Counter = Counter()
    for r in dataset:
        if r["CUST_ID"] in child_pks:
            continue
        if len(r["CUST_NAME"].strip()) > 40:
            exp["ERR_LENGTH_NAME_ORG1"] += 1
        if not r["CITY"].strip():
            exp["ERR_MANDATORY_CITY1"] += 1
        country = r["COUNTRY"].strip()
        if not country:
            exp["ERR_MANDATORY_COUNTRY"] += 1
        elif country.upper() not in COUNTRY_SEED:
            exp["ERR_LOOKUP_FAILED_COUNTRY"] += 1
        pay = r["PAYMENT_TERMS"].strip()
        if pay and pay.upper() not in PAYMENT_TERMS_SEED:
            exp["ERR_LOOKUP_FAILED_ZTERM"] += 1
    return exp


# ------------------------------------------------------------------ severity / signatures
def test_signature_and_severity_rules_are_deterministic():
    assert signature_for(FailureKind.MANDATORY, "NAME_ORG1") == "ERR_MANDATORY_NAME_ORG1"
    assert signature_for(FailureKind.LOOKUP, "COUNTRY", "ADRC") == "ERR_LOOKUP_FAILED_ADRC_COUNTRY"
    assert kind_for_signature("ERR_LOOKUP_FAILED_COUNTRY") == FailureKind.LOOKUP
    assert kind_for_signature("SOMETHING_ELSE") is None
    assert severity_for_signature("ERR_MANDATORY_CITY1") == Severity.CRITICAL
    assert severity_for_signature("ERR_LOOKUP_FAILED_ZTERM") == Severity.CRITICAL
    assert severity_for_signature("ERR_LENGTH_NAME_ORG1") == Severity.HIGH
    assert severity_for_signature("ERR_CHECKTABLE_COUNTRY") == Severity.HIGH
    assert severity_for_signature("UNKNOWN_SIGNATURE") == Severity.HIGH


# ------------------------------------------------------------------ 1. validation engine
def test_validation_on_real_pipeline_output_matches_independent_model(pipeline, dataset):
    run, report, _ = pipeline
    dedup = run.deduplication
    children = {r["_SRC_PK"] for r in read_parquet(run.deduplicated_path) if r["is_duplicate_child"]}
    assert len(children) == dedup.duplicate_children > 0

    assert report.input_records == len(dataset)
    assert report.duplicate_children_excluded == len(children)
    assert report.evaluated_records == len(dataset) - len(children)
    assert report.valid_records + report.defective_records == report.evaluated_records
    assert report.quality_score == round(100.0 * report.valid_records / report.evaluated_records, 2)

    expected = expected_signatures(dataset, children)
    assert {f.signature: f.violation_count for f in report.failures} == dict(expected)
    assert expected_defective(dataset, children) == report.defective_records

    by_sig = {f.signature: f for f in report.failures}
    assert by_sig["ERR_MANDATORY_CITY1"].severity == Severity.CRITICAL
    assert by_sig["ERR_LENGTH_NAME_ORG1"].severity == Severity.HIGH
    assert by_sig["ERR_LOOKUP_FAILED_COUNTRY"].target == "ADRC.COUNTRY"
    assert report.checked_domains == ["T005", "T052", "TB0BK"] and report.unchecked_check_tables == ["TB001"]
    for f in report.failures:
        assert 1 <= len(f.sample_failing_pks) <= 5 and f.violation_count >= len(f.sample_failing_pks)


def expected_defective(dataset, children) -> int:
    from tests.test_module4_transform_dedupe import expected_errors
    return sum(1 for r in dataset if r["CUST_ID"] not in children and expected_errors(r))


def test_duplicates_are_segregated_from_the_valid_payload(pipeline):
    run, report, _ = pipeline
    valid, rejected, dupes = (read_parquet(p) for p in (report.valid_path, report.rejected_path, report.duplicates_path))
    assert len(valid) == report.valid_records and len(rejected) == report.defective_records
    assert len(dupes) == report.duplicate_children_excluded == run.deduplication.duplicate_children
    assert all(r["is_duplicate_child"] for r in dupes)
    assert not any(r["is_duplicate_child"] for r in valid + rejected)
    assert all(r["_TRANSFORM_STATUS"] == "OK" and "_VALIDATION_ERRORS" not in r for r in valid)
    assert all(r["_VALIDATION_ERRORS"] for r in rejected)
    pks = [{r["_SRC_PK"] for r in rows} for rows in (valid, rejected, dupes)]
    assert not (pks[0] & pks[1]) and not (pks[0] & pks[2]) and not (pks[1] & pks[2])
    assert sum(len(p) for p in pks) == report.input_records


def test_each_check_type_on_handcrafted_staging(env, session_factory, tmp_path):
    staging = write_staging(tmp_path / "s.parquet", [
        {"pk": "R01"},
        {"pk": "R02", "set": {"BUT000_NAME_ORG1": "X" * 41}},  # length / truncation risk
        {"pk": "R03", "set": {"BUT000_NAME_ORG1": "   "}},  # whitespace-only mandatory
        {"pk": "R04", "set": {"BUT000_NAME_ORG1": None}},  # NULL mandatory
        {"pk": "R05", "set": {"ADRC_COUNTRY": "ZZ"}},  # not in T005
        {"pk": "R06", "set": {"KNB1_ZTERM": "BAD1"}},  # not in T052
        {"pk": "R07", "set": {"ADRC_COUNTRY": "LOOKUP_FAILED"}},  # sentinel value
        {"pk": "R08", "set": {"BUT000_NAME_ORG1": None}, "dup": True},  # duplicate child: excluded
        {"pk": "R09", "set": {"ADRC_CITY1": "", "ADRC_COUNTRY": "ZZ"}},  # two failures, one record
        {"pk": "R10", "set": {"KNB1_ZTERM": None}, "errors": "KNB1.ZTERM: LOOKUP_FAILED: no mapping for 'NET99'"},
    ])
    report = ValidationEngine(session_factory).validate(env["mapping"].id, staging, tmp_path / "v")
    assert (report.input_records, report.duplicate_children_excluded, report.evaluated_records) == (10, 1, 9)
    assert (report.valid_records, report.defective_records, report.quality_score) == (1, 8, 11.11)
    counts = {f.signature: f.violation_count for f in report.failures}
    assert counts == {
        "ERR_MANDATORY_NAME_ORG1": 2, "ERR_CHECKTABLE_COUNTRY": 2, "ERR_LENGTH_NAME_ORG1": 1,
        "ERR_CHECKTABLE_ZTERM": 1, "ERR_LOOKUP_FAILED_COUNTRY": 1, "ERR_LOOKUP_FAILED_ZTERM": 1,
        "ERR_MANDATORY_CITY1": 1,
    }
    by_sig = {f.signature: f for f in report.failures}
    assert by_sig["ERR_MANDATORY_NAME_ORG1"].sample_failing_pks == ["R03", "R04"]
    assert "R08" not in by_sig["ERR_MANDATORY_NAME_ORG1"].sample_failing_pks  # duplicate not a defect
    rejected = {r["_SRC_PK"]: r["_VALIDATION_ERRORS"] for r in read_parquet(report.rejected_path)}
    assert set(rejected["R09"].split("; ")) == {"ERR_MANDATORY_CITY1", "ERR_CHECKTABLE_COUNTRY"}
    assert [r["_SRC_PK"] for r in read_parquet(report.valid_path)] == ["R01"]
    assert [r["_SRC_PK"] for r in read_parquet(report.duplicates_path)] == ["R08"]


def test_sample_ids_are_capped_at_five(env, session_factory, tmp_path):
    staging = write_staging(tmp_path / "s.parquet", [{"pk": f"P{i:02d}", "set": {"ADRC_CITY1": None}} for i in range(1, 13)])
    report = ValidationEngine(session_factory).validate(env["mapping"].id, staging)
    (failure,) = report.failures
    assert failure.violation_count == 12 and failure.sample_failing_pks == ["P01", "P02", "P03", "P04", "P05"]
    assert report.valid_path is None  # no output_dir -> nothing written


def test_conversion_errors_from_the_transform_stage_are_reported(env, session_factory, tmp_path):
    staging = write_staging(tmp_path / "s.parquet", [
        {"pk": "C1", "set": {"ADRC_POST_CODE1": None}, "errors": "ADRC.POST_CODE1: CONVERSION_FAILED: 'abc' is not valid CHAR(10)"},
        {"pk": "C2", "set": {"BUT000_NAME_ORG1": None},
         "errors": "BUT000.NAME_ORG1: LENGTH_EXCEEDED: value has 60 chars, CHAR(40) allows 40"},
    ])
    report = ValidationEngine(session_factory).validate(env["mapping"].id, staging)
    counts = {f.signature: f.violation_count for f in report.failures}
    # the true cause is reported - a too-long name is LENGTH, not a confusing MANDATORY
    assert counts == {"ERR_CONVERSION_POST_CODE1": 1, "ERR_LENGTH_NAME_ORG1": 1}


def test_validation_input_errors(env, session_factory, tmp_path):
    engine = ValidationEngine(session_factory)
    with pytest.raises(FileNotFoundError):
        engine.validate(env["mapping"].id, tmp_path / "none.parquet")
    with pytest.raises(NotFoundError):
        engine.validate(999_999, write_staging(tmp_path / "a.parquet", [{"pk": "1"}]))
    bad = tmp_path / "bad.parquet"
    duckdb.connect().execute(f"COPY (SELECT 'x' AS _SRC_PK, 'y' AS BUT000_NAME_ORG1) TO '{bad.as_posix()}' (FORMAT parquet)")
    with pytest.raises(ValueError, match="missing columns"):
        engine.validate(env["mapping"].id, bad)


def test_pre_dedup_staging_without_duplicate_flag_validates(env, session_factory, csv_path, tmp_path):
    result = TransformationEngine(session_factory).transform(env["mapping"].id, csv_path, tmp_path / "raw.parquet")
    report = ValidationEngine(session_factory).validate(env["mapping"].id, result.output_path)
    assert report.duplicate_children_excluded == 0 and report.evaluated_records == result.source_records


# ------------------------------------------------------------------ domain providers
def test_domain_providers_are_pluggable(tmp_path, env, session_factory):
    seed = SeedDomainProvider()
    assert {"US", "DE", "JP"} <= seed.get_domain("T005") and "XX" not in seed.get_domain("T005")
    assert seed.get_domain("TVZBT") == seed.get_domain("T052") and "NT30" in seed.get_domain("T052")
    assert seed.get_domain("UNKNOWN_TABLE") is None

    (tmp_path / "T005.csv").write_text("value\nUS\nCA\n", encoding="utf-8")
    csv_provider = CsvDomainProvider(tmp_path)
    assert csv_provider.get_domain("t005") == frozenset({"US", "CA"}) and csv_provider.get_domain("T052") is None
    chained = ChainedDomainProvider(csv_provider, seed)
    assert chained.get_domain("T005") == frozenset({"US", "CA"}) and "NT30" in chained.get_domain("T052")

    staging = write_staging(tmp_path / "s.parquet", [{"pk": "1"}, {"pk": "2", "set": {"ADRC_COUNTRY": "DE"}}])
    report = ValidationEngine(session_factory, domain_provider=chained).validate(env["mapping"].id, staging)
    assert {f.signature: f.violation_count for f in report.failures} == {"ERR_CHECKTABLE_COUNTRY": 1}  # DE not in CSV


# ------------------------------------------------------------------ 2. defect aggregation
def test_defects_are_aggregated_by_signature_and_linked_to_rules(pipeline, session_factory):
    run, report, env = pipeline
    summary = DefectService(session_factory).record_defects(run.run_id, report)
    assert summary.created == len(report.failures) and summary.updated == 0 and summary.resolved == 0
    assert summary.total_open == len(report.failures)

    rows = defect_rows(session_factory, run.run_id)
    assert {s: d.violation_count for s, d in rows.items()} == {f.signature: f.violation_count for f in report.failures}
    for f in report.failures:
        row = rows[f.signature]
        assert row.field_rule_id == env["rule_ids"][f.target]
        assert row.sample_failing_record_ids == f.sample_failing_pks and len(row.sample_failing_record_ids) <= 5
        assert row.jira_issue_key is None and row.jira_status is None
    with session_factory() as s:
        assert s.get(MigMigrationRun, run.run_id).current_phase == MigrationPhase.VALIDATE

    listed = DefectService(session_factory).get_defects(run.run_id)
    assert listed[0].severity == Severity.CRITICAL  # most severe first
    assert all(d.is_open for d in listed)


def test_recording_is_idempotent_and_resolves_fixed_defects(pipeline, session_factory, tmp_path):
    run, report, env = pipeline
    svc = DefectService(session_factory)
    svc.record_defects(run.run_id, report)
    with session_factory() as s:
        before = s.scalar(sa.select(sa.func.count()).select_from(GovDefectAggregate))
    svc.set_jira_link(defect_rows(session_factory, run.run_id)["ERR_MANDATORY_CITY1"].id, "MIG-7", "OPEN")

    again = svc.record_defects(run.run_id, report)  # same report: nothing new
    assert (again.created, again.updated, again.resolved) == (0, len(report.failures), 0)
    with session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(GovDefectAggregate)) == before
    city = defect_rows(session_factory, run.run_id)["ERR_MANDATORY_CITY1"]
    assert (city.jira_issue_key, city.jira_status) == ("MIG-7", "OPEN")  # Jira link survives re-validation

    # Re-validate the *valid* payload only: every previous defect is now resolved (count 0, row kept).
    clean = ValidationEngine(session_factory).validate(env["mapping"].id, report.valid_path)
    assert clean.failures == [] and clean.quality_score == 100.0
    final = svc.record_defects(run.run_id, clean)
    assert (final.created, final.updated, final.resolved, final.total_open) == (0, 0, len(report.failures), 0)
    rows = defect_rows(session_factory, run.run_id)
    assert len(rows) == len(report.failures) and all(d.violation_count == 0 for d in rows.values())
    assert rows["ERR_MANDATORY_CITY1"].jira_issue_key == "MIG-7"
    assert svc.get_defects(run.run_id, open_only=True) == []


def test_count_updates_when_the_data_changes(env, session_factory, tmp_path):
    run_id = make_run(session_factory, env["mapping"].id)
    svc, engine = DefectService(session_factory), ValidationEngine(session_factory)
    rows5 = [{"pk": f"A{i}", "set": {"ADRC_CITY1": None}} for i in range(5)]
    svc.record_defects(run_id, engine.validate(env["mapping"].id, write_staging(tmp_path / "a.parquet", rows5)))
    assert defect_rows(session_factory, run_id)["ERR_MANDATORY_CITY1"].violation_count == 5
    rows2 = rows5[:2]
    summary = svc.record_defects(run_id, engine.validate(env["mapping"].id, write_staging(tmp_path / "b.parquet", rows2)))
    assert summary.updated == 1 and summary.created == 0
    assert defect_rows(session_factory, run_id)["ERR_MANDATORY_CITY1"].violation_count == 2


def test_defect_recording_validates_inputs(env, session_factory, tmp_path):
    svc, engine = DefectService(session_factory), ValidationEngine(session_factory)
    report = engine.validate(env["mapping"].id, write_staging(tmp_path / "a.parquet", [{"pk": "1", "set": {"ADRC_CITY1": None}}]))
    with pytest.raises(NotFoundError):
        svc.record_defects(999_999, report)
    other = svc_other_mapping(session_factory, env)
    with pytest.raises(ValueError, match="mapping set"):
        svc.record_defects(make_run(session_factory, other), report)
    bad = report.model_copy(update={"failures": [report.failures[0].model_copy(update={"field_rule_id": 999_999})]})
    with pytest.raises(ValueError, match="outside the run's mapping set"):
        svc.record_defects(make_run(session_factory, env["mapping"].id), bad)


def svc_other_mapping(session_factory, env) -> int:
    with session_factory.begin() as s:
        base = s.get(MapTableRule, env["mapping"].id)
        other = MapTableRule(source_table_id=base.source_table_id, target_table_id=base.target_table_id,
                             version=99, created_by="someone")
        s.add(other)
        s.flush()
        return other.id


# ------------------------------------------------------------------ 3. Jira
class FakeJira:
    def __init__(self, first_key=1042, status="To Do", fail_paths=(), fail_status=500):
        self.requests: list[httpx.Request] = []
        self.counter, self.status = first_key, status
        self.fail_paths, self.fail_status = set(fail_paths), fail_status
        self.failures_left: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = request.content.decode() if request.content else ""
        for marker in self.fail_paths:
            if marker in body or marker in request.url.path:
                if self.fail_status in (401, 403):
                    return httpx.Response(self.fail_status, text="nope")
                left = self.failures_left.setdefault(marker, 2)
                if left > 0:
                    self.failures_left[marker] = left - 1
                    return httpx.Response(self.fail_status, text="boom")
        if request.method == "POST":
            key = f"MIG-{self.counter}"
            self.counter += 1
            return httpx.Response(201, json={"id": "10001", "key": key})
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, json={"fields": {"status": {"name": self.status}}})

    def by_method(self, method):
        return [r for r in self.requests if r.method == method]


CONFIG = JiraConfig(url="https://jira.example.test", project_key="MIG", api_token="s3cret")


def jira(session_factory, fake, config=CONFIG):
    return JiraService(session_factory, config=config, transport=httpx.MockTransport(fake), retry_delay=0)


@pytest.fixture()
def recorded(pipeline, session_factory):
    run, report, env = pipeline
    DefectService(session_factory).record_defects(run.run_id, report)
    return run, report, env


def test_jira_config_from_environment():
    assert JiraConfig.from_env({}) is None
    assert JiraConfig.from_env({"JIRA_URL": "https://j", "JIRA_API_TOKEN": "t"}) is None  # project missing
    cfg = JiraConfig.from_env({"JIRA_URL": "https://j/", "JIRA_API_TOKEN": "t", "JIRA_PROJECT_KEY": "MIG",
                               "JIRA_USER_EMAIL": "me@x.com", "JIRA_ISSUE_TYPE": "Task"})
    assert (cfg.url, cfg.project_key, cfg.user_email, cfg.issue_type) == ("https://j", "MIG", "me@x.com", "Task")
    assert "t" not in repr(cfg).replace("MIG", "").replace("https", "") or "api_token" not in repr(cfg)  # token not in repr


def test_sync_creates_one_issue_per_open_defect(recorded, session_factory):
    run, report, _ = recorded
    fake = FakeJira()
    keys = jira(session_factory, fake).sync_defects_to_jira(run.run_id)

    assert len(keys) == len(report.failures) and keys == sorted(set(keys), key=keys.index)
    assert set(keys) == {f"MIG-{1042 + i}" for i in range(len(report.failures))}
    posts = fake.by_method("POST")
    assert len(posts) == len(report.failures) and all(r.url.path == "/rest/api/2/issue" for r in posts)
    assert all(r.headers["Authorization"] == "Bearer s3cret" for r in posts)

    import json
    payloads = {json.loads(r.content)["fields"]["summary"]: json.loads(r.content)["fields"] for r in posts}
    city = next(f for f in report.failures if f.signature == "ERR_MANDATORY_CITY1")
    fields = next(v for k, v in payloads.items() if "ERR_MANDATORY_CITY1" in k)
    assert fields["project"] == {"key": "MIG"} and fields["issuetype"] == {"name": "Bug"}
    assert f"{city.violation_count} record(s) failing on ADRC.CITY1" in fields["summary"] and "[CRITICAL]" in fields["summary"]
    assert "Root cause" in fields["description"] and "mandatory in SAP" in fields["description"]
    assert f"|Failing records|{city.violation_count}|" in fields["description"]
    assert all(f"* {pk}" in fields["description"] for pk in city.sample_failing_pks)
    assert f"|Migration run|{run.run_id}|" in fields["description"]
    assert {"data-migration", "severity-critical", "err_mandatory_city1"} <= set(fields["labels"])

    rows = defect_rows(session_factory, run.run_id)
    assert {d.jira_issue_key for d in rows.values()} == set(keys)
    assert all(d.jira_status == "OPEN" for d in rows.values())


def test_second_sync_updates_instead_of_duplicating(recorded, session_factory):
    run, report, _ = recorded
    fake = FakeJira(status="In Progress")
    service = jira(session_factory, fake)
    first = service.sync_defects_to_jira(run.run_id)
    fake.requests.clear()
    second = service.sync_defects_to_jira(run.run_id)
    assert second == first and not fake.by_method("POST")
    assert len(fake.by_method("PUT")) == len(first) and len(fake.by_method("GET")) == len(first)
    assert {d.jira_status for d in defect_rows(session_factory, run.run_id).values()} == {"IN PROGRESS"}


def test_closed_tickets_are_skipped_and_tickets_are_reused_across_runs(recorded, session_factory):
    run, report, env = recorded
    fake = FakeJira()
    first = jira(session_factory, fake).sync_defects_to_jira(run.run_id)

    run2 = make_run(session_factory, env["mapping"].id)
    DefectService(session_factory).record_defects(run2, report)  # same failures in a later run
    fake.requests.clear()
    reused = jira(session_factory, fake).sync_defects_to_jira(run2)
    assert sorted(reused) == sorted(first) and not fake.by_method("POST")  # same tickets, no duplicates

    # a ticket closed in Jira is left alone
    closed = defect_rows(session_factory, run2)["ERR_MANDATORY_CITY1"]
    DefectService(session_factory).set_jira_link(closed.id, closed.jira_issue_key, "DONE")
    fake.requests.clear()
    keys = jira(session_factory, fake).sync_defects_to_jira(run2)
    assert closed.jira_issue_key not in keys and len(keys) == len(first) - 1


def test_resolved_defects_are_not_synced(recorded, session_factory):
    run, report, env = recorded
    clean = ValidationEngine(session_factory).validate(env["mapping"].id, report.valid_path)
    DefectService(session_factory).record_defects(run.run_id, clean)
    fake = FakeJira()
    assert jira(session_factory, fake).sync_defects_to_jira(run.run_id) == [] and not fake.requests


def test_dry_run_without_credentials_touches_nothing(recorded, session_factory, monkeypatch):
    run, report, _ = recorded
    for var in ("JIRA_URL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.delenv(var, raising=False)
    service = JiraService(session_factory)
    assert service.dry_run
    keys = service.sync_defects_to_jira(run.run_id)
    assert len(keys) == len(report.failures) and all(k.startswith("DRY-RUN:ERR_") for k in keys)
    assert len(service.last_payloads) == len(keys) and all(p["summary"] and p["description"] for p in service.last_payloads)
    assert all(d.jira_issue_key is None and d.jira_status is None for d in defect_rows(session_factory, run.run_id).values())


def test_service_reads_credentials_from_env(recorded, session_factory, monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://env.example.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ENV")
    monkeypatch.setenv("JIRA_USER_EMAIL", "bot@example.test")
    run, _, _ = recorded
    fake = FakeJira()
    service = JiraService(session_factory, transport=httpx.MockTransport(fake), retry_delay=0)
    assert not service.dry_run
    service.sync_defects_to_jira(run.run_id)
    first = fake.by_method("POST")[0]
    assert first.headers["Authorization"].startswith("Basic ") and str(first.url).startswith("https://env.example.test/")
    assert b'"key": "ENV"' in first.content or b'"key":"ENV"' in first.content


def test_jira_failures_are_isolated_retried_and_auth_aborts(recorded, session_factory):
    run, report, _ = recorded
    # one defect fails permanently (500 twice) -> collected; the rest sync
    fake = FakeJira(fail_paths=["ERR_MANDATORY_CITY1"])
    service = jira(session_factory, fake)
    keys = service.sync_defects_to_jira(run.run_id)
    assert len(keys) == len(report.failures) - 1
    assert len(service.last_errors) == 1 and "ERR_MANDATORY_CITY1" in service.last_errors[0]
    assert defect_rows(session_factory, run.run_id)["ERR_MANDATORY_CITY1"].jira_issue_key is None

    # transient 500 then success -> retried once
    flaky = FakeJira(fail_paths=["ERR_MANDATORY_CITY1"])
    flaky.failures_left["ERR_MANDATORY_CITY1"] = 1
    keys = jira(session_factory, flaky).sync_defects_to_jira(run.run_id)
    assert len(keys) == len(report.failures)  # the 5 linked tickets are updated, the failed one now created
    assert sum(1 for r in flaky.by_method("POST") if b"ERR_MANDATORY_CITY1" in r.content) == 2  # 500, then success
    assert defect_rows(session_factory, run.run_id)["ERR_MANDATORY_CITY1"].jira_issue_key is not None

    with pytest.raises(JiraAuthError):
        jira(session_factory, FakeJira(fail_paths=["/rest/api/2/issue"], fail_status=401)).sync_defects_to_jira(
            make_run_with_defects(session_factory, recorded))


def make_run_with_defects(session_factory, recorded) -> int:
    _, report, env = recorded
    run_id = make_run(session_factory, env["mapping"].id)
    DefectService(session_factory).record_defects(run_id, report)
    return run_id


# ------------------------------------------------------------------ 4. governance gate
def submit_and_approve(gov, mapping_id, approver="data.steward"):
    gov.submit_mapping_set(mapping_id, "ai-mapper")
    return gov.approve_mapping_set(mapping_id, approver)


def test_segregation_of_duties_blocks_self_approval_and_allows_a_steward(env, session_factory):
    gov = GovernanceService(session_factory, authorized_approvers={"data.steward", "ai-mapper"})
    mapping_id = env["mapping"].id  # created_by = "ai-mapper"
    with pytest.raises(InvalidTransitionError):
        gov.approve_mapping_set(mapping_id, "data.steward")  # DRAFT cannot jump to APPROVED
    gov.submit_mapping_set(mapping_id, "ai-mapper")

    for author in ("ai-mapper", "  AI-Mapper "):  # exact and case/space-variant self-approval
        with pytest.raises(SegregationOfDutiesError):
            gov.approve_mapping_set(mapping_id, author)
    with session_factory() as s:
        row = s.get(MapTableRule, mapping_id)
        assert row.status == MappingStatus.SUBMITTED and row.approved_by is None

    with pytest.raises(NotAuthorizedError):
        gov.approve_mapping_set(mapping_id, "random.user")

    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    approved = gov.approve_mapping_set(mapping_id, " data.steward ", approved_at=stamp)
    assert approved.status == MappingStatus.APPROVED and approved.approved_by == "data.steward"
    assert approved.approved_at == stamp and approved.created_by == "ai-mapper"
    with pytest.raises(InvalidTransitionError):  # APPROVED is terminal
        gov.submit_mapping_set(mapping_id, "x")
    with pytest.raises(ValueError):
        gov.approve_mapping_set(mapping_id, "  ")


def test_database_constraint_backs_up_segregation_of_duties(env, session_factory):
    with session_factory() as s:
        with pytest.raises(IntegrityError):
            s.execute(sa.update(MapTableRule).where(MapTableRule.id == env["mapping"].id).values(
                status=MappingStatus.APPROVED, approved_by="ai-mapper", approved_at=datetime.now(timezone.utc)))


def test_reject_and_rework_cycle(env, session_factory):
    gov = GovernanceService(session_factory)
    mid = env["mapping"].id
    gov.submit_mapping_set(mid, "ai-mapper")
    with pytest.raises(SegregationOfDutiesError):
        gov.reject_mapping_set(mid, "ai-mapper")
    assert gov.reject_mapping_set(mid, "reviewer").status == MappingStatus.REJECTED
    with pytest.raises(InvalidTransitionError):
        gov.approve_mapping_set(mid, "reviewer")
    assert gov.return_to_draft(mid, "ai-mapper").status == MappingStatus.DRAFT
    with pytest.raises(NotFoundError):
        gov.submit_mapping_set(999_999, "x")


def test_gate_blocks_on_critical_defects_until_resolved(pipeline, session_factory):
    run, report, env = pipeline
    gov, defects = GovernanceService(session_factory), DefectService(session_factory)

    # nothing validated, mapping still DRAFT
    early = gov.evaluate_readiness(run.run_id)
    assert early.state == GateState.BLOCKED and not early.validated
    assert any("must be APPROVED" in r for r in early.reasons) and any("validation has not been recorded" in r for r in early.reasons)

    defects.record_defects(run.run_id, report)
    submit_and_approve(gov, env["mapping"].id)
    blocked = gov.evaluate_readiness(run.run_id)
    critical = [f for f in report.failures if f.severity == Severity.CRITICAL]
    assert blocked.state == GateState.BLOCKED and blocked.validated and blocked.mapping_status == "APPROVED"
    assert blocked.open_critical_defects == len(critical) > 0
    assert blocked.open_defects == len(report.failures)
    assert len(blocked.reasons) == 1 and "CRITICAL" in blocked.reasons[0]
    assert all(f.signature in blocked.reasons[0] for f in critical)
    with pytest.raises(GateBlockedError) as exc:
        gov.require_ready_for_load(run.run_id)
    assert exc.value.result.open_critical_defects == len(critical)

    # a Jira ticket marked Done does not clear data that is still bad
    row = defect_rows(session_factory, run.run_id)[critical[0].signature]
    defects.set_jira_link(row.id, "MIG-1", "DONE")
    assert gov.evaluate_readiness(run.run_id).state == GateState.BLOCKED

    # fix the data: re-validate the valid payload -> defects resolve -> gate opens
    clean = ValidationEngine(session_factory).validate(env["mapping"].id, report.valid_path)
    defects.record_defects(run.run_id, clean)
    ready = gov.require_ready_for_load(run.run_id)
    assert ready.ready and ready.state == GateState.READY_FOR_LOAD and ready.reasons == []
    assert ready.open_critical_defects == 0 and ready.open_defects == 0
    with session_factory() as s:  # READY_FOR_LOAD is computed, never persisted
        assert s.get(MigMigrationRun, run.run_id).status == RunStatus.SUCCEEDED


def test_non_critical_defects_do_not_block_the_gate(env, session_factory, tmp_path):
    gov, defects = GovernanceService(session_factory), DefectService(session_factory)
    run_id = make_run(session_factory, env["mapping"].id)
    staging = write_staging(tmp_path / "s.parquet", [{"pk": "1"}, {"pk": "2", "set": {"ADRC_COUNTRY": "ZZ"}},
                                                     {"pk": "3", "set": {"BUT000_NAME_ORG1": "Y" * 50}}])
    report = ValidationEngine(session_factory).validate(env["mapping"].id, staging)
    assert {f.severity for f in report.failures} == {Severity.HIGH}
    defects.record_defects(run_id, report)
    submit_and_approve(gov, env["mapping"].id)
    result = gov.evaluate_readiness(run_id)
    assert result.ready and result.open_defects == 2 and result.open_critical_defects == 0


def test_gate_requires_a_successful_validated_run(env, session_factory, tmp_path):
    gov, defects = GovernanceService(session_factory), DefectService(session_factory)
    submit_and_approve(gov, env["mapping"].id)
    failed_run = make_run(session_factory, env["mapping"].id, status=RunStatus.FAILED)
    clean = ValidationEngine(session_factory).validate(env["mapping"].id, write_staging(tmp_path / "c.parquet", [{"pk": "1"}]))
    defects.record_defects(failed_run, clean)
    result = gov.evaluate_readiness(failed_run)
    assert not result.ready and any("must be SUCCEEDED" in r for r in result.reasons)

    unvalidated = make_run(session_factory, env["mapping"].id)
    assert any("validation has not been recorded" in r for r in gov.evaluate_readiness(unvalidated).reasons)
    with pytest.raises(NotFoundError):
        gov.evaluate_readiness(999_999)
