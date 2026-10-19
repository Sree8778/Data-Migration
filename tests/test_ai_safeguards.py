"""Safeguards around the AI mapping: repeat-run stability, evidence checks, model pinning, and the pre-submission dry run.

The model is never trusted. These tests pin down what happens when it is inconsistent (the same column answered two ways), when its
answer contradicts the data (country names copied into a key field), when its model is an alias, and when a mapping that looks fine
would reject every row (a lookup rule with an empty table): the problem is found BEFORE the mapping reaches an approver.
"""

from __future__ import annotations

import json
from collections import defaultdict

import httpx
import pytest

from src.api.settings import Settings
from src.schemas.mapping_ai import FieldMappingRecommendation, ProposalOutcome, ReviewStatus, SourceColumnContext
from src.services.gemini_key_manager import GeminiKeyManager
from src.services.llm_mapper_service import LLMMapperService, is_pinned_model
from src.services.mapping_orchestrator import MappingOrchestrator
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.rule_validator import RuleValidator
from tests.test_gemini_key_manager import CANDS, CTX, FakeGoogle, K1, ok
from tests.test_module3_mapper import embeddings, encoder, legacy_table, rec  # noqa: F401 - fixtures
from tests.test_module4_transform_dedupe import HEADER, csv_path, dataset, env, expected_errors  # noqa: F401
from tests.test_module7_api import ctx, make_client, make_ctx, subset, upload  # noqa: F401


# ------------------------------------------------------------------ a model that answers as scripted, run after run
class FlakyLLM:
    provider = "gemini"
    configured_model = model_name = "fake-model-1"

    def __init__(self, answers: dict[str, list], deterministic: bool = False) -> None:
        self.answers, self.deterministic = answers, deterministic
        self.calls: dict[str, int] = defaultdict(int)

    def recommend(self, context, candidates):
        sequence = self.answers.get(context.column_name, [None])
        item = sequence[min(self.calls[context.column_name], len(sequence) - 1)]
        self.calls[context.column_name] += 1
        return None if item is None else FieldMappingRecommendation(**item)


def generate(session_factory, embeddings, llm, table_id, runs):
    orchestrator = MappingOrchestrator(session_factory, embeddings, llm, RuleValidator(session_factory))
    report = orchestrator.generate_table_mapping_report(table_id, consistency_runs=runs)
    return report, {p.source_column: p for p in report.proposals}


NAME_COPY = rec("BUT000", "NAME_ORG1", 0.9)
NAME_SQL = rec("BUT000", "NAME_ORG1", 0.9, "SQL_EXPRESSION", "UPPER(TRIM(CUST_NAME))")
CITY_COPY = rec("ADRC", "CITY1", 0.95)


# ------------------------------------------------------------------ 1. stability across repeated runs
def test_unanimous_answers_are_asked_for_every_run_and_not_flagged(session_factory, embeddings, legacy_table):
    llm = FlakyLLM({"POSTAL_CODE": [rec("ADRC", "POST_CODE1", 0.9)]})
    report, p = generate(session_factory, embeddings, llm, legacy_table.id, runs=3)
    assert llm.calls["POSTAL_CODE"] == 3 and report.consistency_runs == 3
    assert p["POSTAL_CODE"].outcome == ProposalOutcome.SAVED and p["POSTAL_CODE"].runs == 3 and p["POSTAL_CODE"].agreement == 1.0
    assert not any("unstable" in r for r in p["POSTAL_CODE"].reasons) and p["POSTAL_CODE"].alternatives == []


def test_a_column_whose_answers_differ_is_flagged_and_the_majority_answer_is_kept(session_factory, embeddings, legacy_table):
    llm = FlakyLLM({"CUST_NAME": [NAME_COPY, NAME_SQL, NAME_COPY]})
    report, p = generate(session_factory, embeddings, llm, legacy_table.id, runs=3)
    name = p["CUST_NAME"]
    assert name.outcome == ProposalOutcome.SAVED and name.recommendation.rule_type == "DIRECT_COPY"  # 2 of 3 won
    assert name.agreement == pytest.approx(2 / 3) and name.runs == 3 and name.status == ReviewStatus.NEEDS_REVIEW
    assert "unstable: 2 of 3 runs gave BUT000.NAME_ORG1 (DIRECT_COPY), the others gave 1 x BUT000.NAME_ORG1 (SQL_EXPRESSION)" in name.reasons
    assert name.alternatives == ["1 x BUT000.NAME_ORG1 (SQL_EXPRESSION)"]
    # the flag travels with the stored rule, so the Studio shows it and the approver sees it
    from src.db.models import MapFieldRule
    with session_factory() as s:
        rule = s.get(MapFieldRule, name.field_rule_id)
        assert rule.ai_reasoning.startswith("[NEEDS_REVIEW]") and "unstable: 2 of 3 runs" in rule.ai_reasoning


def test_a_tie_keeps_the_more_confident_answer_but_still_flags_it(session_factory, embeddings, legacy_table):
    llm = FlakyLLM({"CUST_NAME": [rec("BUT000", "NAME_ORG1", 0.70), CITY_COPY]})
    _, p = generate(session_factory, embeddings, llm, legacy_table.id, runs=2)
    name = p["CUST_NAME"]
    assert name.recommendation.target_column == "CITY1" and name.agreement == 0.5 and name.status == ReviewStatus.NEEDS_REVIEW
    assert any(r.startswith("unstable: 1 of 2 runs gave ADRC.CITY1") for r in name.reasons)


def test_no_match_in_one_run_does_not_hide_a_real_mapping_but_a_majority_of_no_match_wins(session_factory, embeddings, legacy_table):
    _, tie = generate(session_factory, embeddings, FlakyLLM({"CUST_NAME": [None, NAME_COPY]}), legacy_table.id, runs=2)
    assert tie["CUST_NAME"].outcome == ProposalOutcome.SAVED and tie["CUST_NAME"].status == ReviewStatus.NEEDS_REVIEW  # kept, flagged
    _, majority = generate(session_factory, embeddings, FlakyLLM({"CUST_NAME": [None, None, NAME_COPY]}), legacy_table.id, runs=3)
    name = majority["CUST_NAME"]
    assert name.outcome == ProposalOutcome.NO_MATCH and name.status == ReviewStatus.NEEDS_REVIEW  # nothing saved, but a human is told why
    assert name.alternatives == ["1 x BUT000.NAME_ORG1 (DIRECT_COPY)"] and name.reasons[0].startswith("unstable: 2 of 3 runs gave NO_MATCH")


def test_a_deterministic_local_model_is_asked_once_however_many_runs_are_requested(session_factory, embeddings, legacy_table):
    llm = FlakyLLM({"CUST_NAME": [NAME_COPY]}, deterministic=True)
    report, p = generate(session_factory, embeddings, llm, legacy_table.id, runs=5)
    assert llm.calls["CUST_NAME"] == 1 and report.consistency_runs == 1 and p["CUST_NAME"].runs == 1


def test_the_server_setting_is_the_default_and_is_bounded():
    assert Settings.from_env({"MIGRATION_AI_CONSISTENCY_RUNS": "3"}).ai_consistency_runs == 3
    assert Settings.from_env({"MIGRATION_AI_CONSISTENCY_RUNS": "99"}).ai_consistency_runs == 5
    assert Settings.from_env({}).ai_consistency_runs == 2 and Settings.from_env({}).preflight == "enforce"
    with pytest.raises(ValueError):
        Settings.from_env({"MIGRATION_PREFLIGHT": "sometimes"})


# ------------------------------------------------------------------ 2. evidence from the data
def _source(name, length, values):
    return SourceColumnContext(column_id=1, column_name=name, data_type="VARCHAR", char_max_length=length, evidence_values=values, sample_values=values[:3])


def test_copying_country_names_into_a_key_field_is_flagged_but_valid_keys_and_lookups_are_not(session_factory, embeddings):
    validator = RuleValidator(session_factory)
    direct = FieldMappingRecommendation(**rec("ADRC", "COUNTRY", 0.95))
    bad = validator.validate(direct, source=_source("LAND", 20, ["Germany", "France", "DE"]))
    assert "only 1 of 3 sample values are valid T005 keys, so a lookup may be needed" in bad.reasons and bad.status == ReviewStatus.NEEDS_REVIEW
    assert validator.validate(direct, source=_source("LAND", 2, ["DE", "fr", "US"])).reasons == []  # real keys, any case
    lookup = FieldMappingRecommendation(**rec("ADRC", "COUNTRY", 0.95, "VALUE_MAPPING"))
    assert not any("T005" in r for r in validator.validate(lookup, source=_source("LAND", 20, ["Germany"])).reasons)  # a lookup is trusted to translate


def test_a_source_longer_than_the_sap_field_is_flagged_for_a_plain_copy_only(session_factory, embeddings):
    validator = RuleValidator(session_factory)
    copy = validator.validate(FieldMappingRecommendation(**NAME_COPY), source=_source("NAME1", 60, ["Acme"]))
    assert "source values reach 60 characters but NAME_ORG1 holds 40" in copy.reasons
    shortened = validator.validate(FieldMappingRecommendation(**NAME_SQL), source=_source("NAME1", 60, ["Acme"]))
    assert not any("characters" in r for r in shortened.reasons)  # an expression is trusted to shorten
    assert not any("characters" in r for r in validator.validate(FieldMappingRecommendation(**NAME_COPY), source=_source("NAME1", 40, ["Acme"])).reasons)


# ------------------------------------------------------------------ 3. which model answered
def _gemini(model: str, resolved: str | None) -> LLMMapperService:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = ok().json()
        if resolved:
            answer["modelVersion"] = resolved
        return httpx.Response(200, json=answer)
    mgr = GeminiKeyManager(keys=[K1], model=model, transport=httpx.MockTransport(handler))
    return LLMMapperService(client=httpx.Client(base_url="http://ollama.test/v1"), provider="gemini", gemini=mgr)


def test_the_concrete_model_behind_an_alias_is_recorded_and_the_alias_is_reported_unpinned():
    alias = _gemini("gemini-flash-latest", "gemini-9-flash-001")
    alias.recommend(CTX, CANDS)
    assert alias.model_name == "gemini-flash-latest -> gemini-9-flash-001" and is_pinned_model(alias) is False
    pinned = _gemini("gemini-9-flash-001", "gemini-9-flash-001")
    pinned.recommend(CTX, CANDS)
    assert pinned.model_name == "gemini-9-flash-001" and is_pinned_model(pinned) is True
    silent = _gemini("gemini-9-flash-001", None)  # a reply without modelVersion still works
    silent.recommend(CTX, CANDS)
    assert silent.model_name == "gemini-9-flash-001"


def test_ollama_names_without_a_tag_count_as_unpinned():
    llm = LLMMapperService(client=httpx.Client(base_url="http://x/v1"), model="qwen2.5-coder")
    assert is_pinned_model(llm) is False
    assert is_pinned_model(LLMMapperService(client=httpx.Client(base_url="http://x/v1"), model="qwen2.5-coder:7b")) is True
    assert is_pinned_model(LLMMapperService(client=httpx.Client(base_url="http://x/v1"), model="qwen2.5-coder:latest")) is False


# ------------------------------------------------------------------ 4. the pre-submission dry run (through the API)
@pytest.fixture()
def landed(ctx, dataset):
    seed_sap_catalog(ctx.metadata)
    client = make_client(ctx)
    table = upload(client, subset(dataset, 300, clean=True)).json()["table_id"]
    assert client.post(f"/api/profile/{table}").status_code == 200
    mset = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
    return client, ctx, table, mset


def add_rule(client, mset, table, column, rule_type, source, lookups=None):
    body = {"target_table": table, "target_column": column, "rule_type": rule_type, "source_column": source}
    if lookups is not None:
        body["lookups"] = lookups
    response = client.post(f"/api/mapping/{mset}/rules", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_an_empty_lookup_blocks_submission_and_the_report_says_what_to_fix(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    response = client.post(f"/api/governance/submit-mapping/{mset}")
    assert response.status_code == 422 and response.json()["code"] == "PREFLIGHT_BLOCKED"
    report = response.json()["detail"]["report"]
    blocker = next(f for f in report["findings"] if f["code"] == "EMPTY_LOOKUP")
    assert blocker["severity"] == "BLOCKER" and blocker["rule_id"] == country["rule_id"] and "every row would be rejected" in blocker["message"]
    assert report["can_submit"] is False and report["sampled"] is True and report["sample_rows"] == report["source_rows"] > 200
    assert client.get(f"/api/mapping/{mset}").json()["mapping"]["status"] == "DRAFT"  # nothing moved
    assert client.get(f"/api/mapping/{mset}/preflight").json()["blockers"] == report["blockers"]  # kept for the approver
    assert client.get("/api/runs").json() == []  # the dry run wrote no run, no defects
    fresh = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
    assert client.get(f"/api/mapping/{fresh}/preflight").json() is None  # nothing checked yet is null, not an error


def test_a_lookup_missing_most_of_the_values_is_blocked_and_the_unmatched_values_are_listed(landed):
    client, ctx, table, mset = landed
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", lookups={"ZZ": "ZZ"})  # the data has none of its values
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    check = next(r for r in report["rules"] if r["rule_id"] == country["rule_id"])
    assert check["verdict"] == "BLOCKER" and check["pass_rate"] == 0.0 and check["failure_kinds"] == {"LOOKUP_FAILED": check["sampled"]} and check["sampled"] > 200
    assert check["unmatched_values"] and check["unmatched_values"][0]["count"] >= check["unmatched_values"][-1]["count"]
    assert sum(u["count"] for u in check["unmatched_values"]) <= check["sampled"]
    assert {f["code"] for f in report["findings"]} >= {"MOSTLY_FAILING", "LOW_YIELD"} and report["valid_rate"] == 0.0
    assert client.post(f"/api/governance/submit-mapping/{mset}").status_code == 422


def test_a_working_mapping_passes_and_is_submitted_with_its_measured_pass_rates(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    ctx.lookups.seed_standard_lookups(country["rule_id"], "country")
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    assert report["can_submit"] is True and report["blockers"] == 0 and report["valid_rate"] == 1.0
    assert {r["target"]: r["pass_rate"] for r in report["rules"]} == {"BUT000.NAME_ORG1": 1.0, "ADRC.COUNTRY": 1.0}
    submitted = client.post(f"/api/governance/submit-mapping/{mset}")
    assert submitted.status_code == 200 and submitted.json()["status"] == "SUBMITTED"
    assert client.get(f"/api/mapping/{mset}/preflight").json()["can_submit"] is True


def test_warn_mode_reports_the_problem_but_lets_the_submission_through_and_off_skips_the_check(session_factory, tmp_path, encoder, dataset):
    for mode, report_kept in (("warn", True), ("off", False)):
        context = make_ctx(session_factory, tmp_path / mode, encoder, preflight=mode)
        seed_sap_catalog(context.metadata)
        client = make_client(context)
        table = upload(client, subset(dataset, 120, clean=True), table=f"T_{mode.upper()}", system=f"SYS_{mode.upper()}").json()["table_id"]
        client.post(f"/api/profile/{table}")
        mset = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
        add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")  # an empty lookup
        assert client.post(f"/api/governance/submit-mapping/{mset}").status_code == 200
        stored = client.get(f"/api/mapping/{mset}/preflight").json()
        assert (stored is not None) is report_kept  # "off" never ran the check, so nothing is stored
        if report_kept:
            assert stored["can_submit"] is False


def test_the_check_without_a_landed_file_still_catches_structural_problems(ctx, landed):
    client, context, table, mset = landed
    add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    report = context.preflight.check(mset, None)
    codes = {f["code"] if isinstance(f, dict) else f.code for f in report.findings}
    assert report.sampled is False and {"EMPTY_LOOKUP", "NOT_SAMPLED"} <= codes and report.can_submit is False
    empty = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
    assert {f.code for f in context.preflight.check(empty, None).findings} >= {"NO_RULES"}


def test_a_rule_reading_a_column_the_file_does_not_have_is_a_blocker(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    report = ctx.preflight.check(mset, ctx.source_file(table), file_columns={"cust_id"})  # pretend the file lost CUST_NAME
    missing = next(f for f in report.findings if f.code == "SOURCE_MISSING")
    assert missing.severity.value == "BLOCKER" and "CUST_NAME" in missing.message and report.sampled is False


def test_the_sample_is_the_same_rows_every_time(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    first = client.post(f"/api/mapping/{mset}/preflight?sample_rows=100").json()
    second = client.post(f"/api/mapping/{mset}/preflight?sample_rows=100").json()
    assert first["sample_rows"] == second["sample_rows"] == 100 and first["rules"] == second["rules"]


def test_placeholder_rules_fail_by_design_so_they_are_reported_but_never_block(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    assert client.post(f"/api/construction/placeholder-rules/{mset}").status_code == 201  # mandatory fields the legacy system never had
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    codes = {f["code"] for f in report["findings"]}
    assert "AWAITING_CONSTRUCTION" in codes and report["can_submit"] is True and report["blockers"] == 0
    assert report["valid_rate"] < 0.5 and "LOW_YIELD" in codes  # the low yield is shown, as a warning
    assert all(f["severity"] == "WARNING" for f in report["findings"] if f["code"] in ("LOW_YIELD", "AWAITING_CONSTRUCTION"))
