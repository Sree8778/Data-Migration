"""Module 3 tests: vector retrieval, AST guardrails, and the end-to-end mapping flow.

The LLM is scripted through an httpx MockTransport speaking the OpenAI-compatible protocol, so
the real client code path (HTTP, JSON, retries, contract validation) is exercised. One optional
live test runs against a real Ollama when `qwen2.5-coder:7b` is installed.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from src.db.enums import MappingStatus, PiiClassification, RuleType, SystemType, TargetType
from src.db.models import CatColumn, MapFieldRule, MapTableRule
from src.schemas.mapping_ai import (
    FieldMappingRecommendation,
    ProposalOutcome,
    ReviewStatus,
    SourceColumnContext,
)
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    DataSourceCreate,
    TableCreate,
)
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.embedding_service import EmbeddingService, SentenceTransformerEncoder
from src.services.llm_mapper_service import (
    DEFAULT_MODEL,
    LLMMapperService,
    LLMOutputError,
    LLMUnavailableError,
)
from src.services.mapping_orchestrator import MappingOrchestrator
from src.services.metadata_service import MappingValidationError, NotFoundError
from src.services.rule_validator import RuleValidator, validate_sql_expression

SOURCE_COLUMNS = ["CUST_ID", "CUST_NAME", "CUSTOMER_TITLE", "COUNTRY_CD", "POSTAL_CODE", "CUST_TYPE",
                  "EMAIL", "LEGACY_NOTES", "FAX_NO", "CREDIT_LIMIT", "MISC_CODE", "GARBAGE_COL"]


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="session")
def encoder():
    enc = SentenceTransformerEncoder()
    try:
        enc.encode(["warm up"])
    except Exception as exc:  # no network / model unavailable
        pytest.skip(f"embedding model unavailable: {exc!r}")
    return enc


@pytest.fixture()
def embeddings(encoder, session_factory, service, tmp_path):
    seed_sap_catalog(service)
    return EmbeddingService(session_factory, encoder=encoder, cache_dir=tmp_path / "emb")


def rec(table, column, conf, rule="DIRECT_COPY", sql=None, reasoning="because"):
    return {"target_table": table, "target_column": column, "confidence_score": conf,
            "rule_type": rule, "transformation_sql": sql, "reasoning": reasoning}


NO_MATCH_REPLY = rec("NO_MATCH", "NO_MATCH", 0, reasoning="nothing fits")

SCRIPT = {
    "CUST_ID": rec("BUT000", "BP_EXT", 0.82, "SQL_EXPRESSION", "LPAD(TRIM(CUST_ID), 10, '0')"),
    "CUST_NAME": rec("BUT000", "NAME_ORG1", 0.93),
    "CUSTOMER_TITLE": rec("BUT000", "NAME_ORG1", 0.70),  # loses to CUST_NAME on the same target
    "COUNTRY_CD": rec("ADRC", "COUNTRY", 0.90, "SQL_EXPRESSION", "UPPER(TRIM(COUNTRY_CD))"),
    "POSTAL_CODE": rec("ADRC", "POST_CODE1", 0.88),
    "CUST_TYPE": rec("BUT000", "TYPE", 0.85, "VALUE_MAPPING"),
    "EMAIL": NO_MATCH_REPLY,
    "LEGACY_NOTES": NO_MATCH_REPLY,
    "FAX_NO": rec("ADRC", "REGION", 0.40),
    "CREDIT_LIMIT": rec("KNB1", "NOPE_COLUMN", 0.95),  # hallucinated target
    "MISC_CODE": rec("KNB1", "ZTERM", 0.90, "SQL_EXPRESSION", "UPPER(MISC_CODE); DROP TABLE cat_columns"),
    "GARBAGE_COL": "this is not json at all",
}


class ScriptedOllama:
    """httpx transport that answers chat completions from SCRIPT, recording every request."""

    def __init__(self, script=None, fail=None):
        self.script, self.fail, self.requests = script or SCRIPT, fail, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise self.fail
        body = json.loads(request.content)
        self.requests.append(body)
        user = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        try:
            column = json.loads(user)["source_column"]["name"]
        except (ValueError, KeyError):  # retry message after an invalid reply
            column = next(json.loads(m["content"])["source_column"]["name"]
                          for m in body["messages"] if m["role"] == "user" and m["content"].startswith("{"))
        reply = self.script[column]
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), base_url="http://ollama.test/v1")


def make_llm(script=None, fail=None):
    transport = ScriptedOllama(script, fail)
    return LLMMapperService(client=transport.client()), transport


@pytest.fixture()
def legacy_table(service, embeddings):
    """Profiled mock legacy Oracle AR_CUSTOMERS (stats as Module 2 would persist them)."""
    source = service.register_data_source(DataSourceCreate(
        system_name="LEGACY_ORACLE_AR", system_type=SystemType.ORACLE, target_type=TargetType.SOURCE))
    table = service.register_table(TableCreate(source_id=source.id, table_name="AR_CUSTOMERS"))

    def col(name, dtype="VARCHAR2", length=50, samples=("A", "B", "C", "D"), pii=PiiClassification.INTERNAL,
            null=0.05, distinct=0.5, profiled=True):
        return ColumnCreate(
            column_name=name, data_type=dtype, char_max_length=length, pii_classification=pii,
            sample_values=list(samples) if profiled else None,
            null_ratio=Decimal(str(null)) if profiled else None,
            distinct_ratio=Decimal(str(distinct)) if profiled else None)

    columns = [col(n) for n in SOURCE_COLUMNS]
    columns[1] = col("CUST_NAME", samples=("Acme GmbH", "Globex", "Initech", "Umbrella"),
                     pii=PiiClassification.CONFIDENTIAL)
    columns[6] = col("EMAIL", samples=("john.doe@example.com", "a@b.org", "x@y.net", "extra@z.io"),
                     pii=PiiClassification.RESTRICTED_PII)
    columns.append(col("UNPROFILED_COL", profiled=False))
    service.bulk_register_columns(BulkColumnRegistration(table_id=table.id, columns=columns))
    return table


def orchestrator(session_factory, embeddings, llm):
    return MappingOrchestrator(session_factory, embeddings, llm, RuleValidator(session_factory))


# ------------------------------------------------------------------ 1. vector retrieval
def test_top_k_candidates_against_sap_dictionary(embeddings):
    top = embeddings.get_top_k_candidates("COUNTRY_CD", "VARCHAR2", top_k=5)
    assert len(top) == 5
    assert [c.score for c in top] == sorted((c.score for c in top), reverse=True)
    assert all(-1 <= c.score <= 1 for c in top)
    assert (top[0].table_name, top[0].column_name) == ("ADRC", "COUNTRY")
    first = top[0]  # vector metadata is carried through
    assert first.data_type == "CHAR" and first.business_name and first.description and first.check_table == "T005"

    assert "POST_CODE1" in [c.column_name for c in embeddings.get_top_k_candidates("POSTAL_CODE", "VARCHAR2", 2)]
    assert "NAME_ORG1" in [c.column_name for c in embeddings.get_top_k_candidates("CUST_NAME", "VARCHAR2", 3)]
    assert "ZTERM" in [c.column_name for c in embeddings.get_top_k_candidates("PAYMENT_TERMS", "VARCHAR2", 2)]


def test_retrieval_is_deterministic_and_validates_top_k(embeddings):
    a = embeddings.get_top_k_candidates("CITY", "VARCHAR2", 5)
    b = embeddings.get_top_k_candidates("CITY", "VARCHAR2", 5)
    assert a == b
    with pytest.raises(ValueError):
        embeddings.get_top_k_candidates("CITY", "VARCHAR2", 0)


def test_embeddings_are_cached_on_disk(encoder, session_factory, service, tmp_path):
    seed_sap_catalog(service)

    class Counting:
        name = encoder.name

        def __init__(self):
            self.docs = 0

        def encode(self, texts, *, is_query=False):
            if not is_query:
                self.docs += len(texts)
            return encoder.encode(texts, is_query=is_query)

    first, second = Counting(), Counting()
    assert EmbeddingService(session_factory, first, tmp_path).build_index() == 30
    assert EmbeddingService(session_factory, second, tmp_path).build_index() == 30
    assert first.docs == 30 and second.docs == 0  # second build served from cache


# ------------------------------------------------------------------ 2. AST guardrails
@pytest.mark.parametrize("sql", [
    "LPAD(TRIM(CUST_ID), 10, '0')",
    "UPPER(COUNTRY_CD)",
    "COALESCE(NULLIF(TRIM(COUNTRY_CD), ''), 'US')",
    "CASE WHEN CUST_TYPE = 'C' THEN '2' ELSE '1' END",
    "CAST(CUST_ID AS VARCHAR)",
    "SUBSTRING(CUST_NAME, 1, 40)",
    "CONCAT(CUST_NAME, ' ', CUST_ID)",
    "NVL(CUST_NAME, 'N/A')",
    "CUST_NAME || '-' || CUST_ID",
    "LPAD(source_col, 10, '0')",
])
def test_valid_expressions_pass(sql):
    out, errors = validate_sql_expression(sql, ["CUST_ID", "CUST_NAME", "COUNTRY_CD", "CUST_TYPE"])
    assert errors == [] and out


@pytest.mark.parametrize("sql", [
    "DROP TABLE cat_columns",
    "UPDATE cat_columns SET data_type = 'x'",
    "INSERT INTO cat_columns VALUES (1)",
    "DELETE FROM cat_columns",
    "TRUNCATE TABLE cat_columns",
    "CREATE TABLE evil (a int)",
    "ALTER TABLE cat_columns ADD c int",
    "COPY cat_columns TO 'out.csv'",
    "CUST_NAME; DROP TABLE cat_columns",
    "1; SELECT 1",
    "CUST_NAME -- trailing comment",
    "CUST_NAME /* hidden */",
    "(SELECT password FROM users)",
    "CUST_NAME UNION SELECT 1",
    "pg_sleep(10)",
    "read_csv('/etc/passwd')",
    "SUM(CUST_ID)",
    "other_table.CUST_NAME",
    "unknown_column",
    "EXEC xp_cmdshell 'dir'",
    "x' OR '1'='1",
    "LPAD(CUST_ID,",
    "",
])
def test_dangerous_or_invalid_sql_is_rejected(sql):
    out, errors = validate_sql_expression(sql, ["CUST_ID", "CUST_NAME", "COUNTRY_CD", "CUST_TYPE"])
    assert out is None and errors


def test_static_value_must_not_reference_columns():
    assert validate_sql_expression("'BP01'", [], allow_columns=False) == ("'BP01'", [])
    out, errors = validate_sql_expression("CUST_NAME", ["CUST_NAME"], allow_columns=False)
    assert out is None and errors


def test_validator_checks_target_sql_and_confidence(session_factory, embeddings):
    v = RuleValidator(session_factory)
    cols = ["CUST_NAME", "CUST_TYPE"]

    ok = v.validate(FieldMappingRecommendation(**rec("BUT000", "NAME_ORG1", 0.9)), allowed_source_columns=cols)
    assert ok.status == ReviewStatus.OK and ok.persistable and ok.target_column_id

    low = v.validate(FieldMappingRecommendation(**rec("but000", "name_org1", 0.59)), allowed_source_columns=cols)
    assert low.status == ReviewStatus.NEEDS_REVIEW and low.persistable  # case-insensitive lookup
    at_threshold = v.validate(FieldMappingRecommendation(**rec("BUT000", "NAME_ORG1", 0.60)), allowed_source_columns=cols)
    assert at_threshold.status == ReviewStatus.OK

    ghost = v.validate(FieldMappingRecommendation(**rec("BUT000", "NO_SUCH", 0.99)), allowed_source_columns=cols)
    assert ghost.status == ReviewStatus.NEEDS_REVIEW and not ghost.persistable and ghost.target_column_id is None

    evil = v.validate(FieldMappingRecommendation(
        **rec("BUT000", "NAME_ORG1", 0.99, "SQL_EXPRESSION", "CUST_NAME; DROP TABLE x")), allowed_source_columns=cols)
    assert evil.status == ReviewStatus.NEEDS_REVIEW and not evil.persistable and evil.transformation_sql is None

    missing = v.validate(FieldMappingRecommendation(**rec("BUT000", "NAME_ORG1", 0.99, "SQL_EXPRESSION")),
                         allowed_source_columns=cols)
    assert not missing.persistable

    vm = v.validate(FieldMappingRecommendation(**rec("BUT000", "TYPE", 0.99, "VALUE_MAPPING")), allowed_source_columns=cols)
    assert vm.status == ReviewStatus.NEEDS_REVIEW and vm.persistable

    # object scoping: a real SAP column outside the requested business object is not a valid target
    assert not RuleValidator(session_factory, object_name="MATERIAL").validate(
        FieldMappingRecommendation(**rec("BUT000", "NAME_ORG1", 0.9))).persistable


def test_recommendation_contract_is_strict():
    with pytest.raises(ValueError):
        FieldMappingRecommendation(**rec("A", "B", 1.2))
    with pytest.raises(ValueError):
        FieldMappingRecommendation(**rec("A", "B", 0.5, rule="PYTHON_SNIPPET"))
    with pytest.raises(ValueError):
        FieldMappingRecommendation(**{**rec("A", "B", 0.5), "unexpected": 1})
    assert FieldMappingRecommendation(**rec("A", "B", 0.5, sql="   ")).transformation_sql is None


# ------------------------------------------------------------------ LLM client
def _ctx(name="CUST_NAME"):
    return SourceColumnContext(column_id=1, column_name=name, data_type="VARCHAR2",
                               null_ratio=0.1, distinct_ratio=0.9, sample_values=["x"])


def test_llm_client_retries_once_then_succeeds():
    replies = iter(["not json", json.dumps(rec("BUT000", "NAME_ORG1", 0.9))])

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    llm = LLMMapperService(client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x/v1"))
    result = llm.recommend(_ctx(), [])
    assert result.target_column == "NAME_ORG1"


def test_llm_client_accepts_fenced_json_and_raises_on_persistent_garbage():
    fenced = "```json\n" + json.dumps(rec("BUT000", "NAME_ORG1", 0.9)) + "\n```"
    llm = LLMMapperService(client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})),
        base_url="http://x/v1"))
    assert llm.recommend(_ctx(), []).confidence_score == 0.9

    bad = LLMMapperService(client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})),
        base_url="http://x/v1"))
    with pytest.raises(LLMOutputError):
        bad.recommend(_ctx(), [])


def test_llm_client_reports_unavailable_ollama():
    llm, _ = make_llm(fail=httpx.ConnectError("refused"))
    with pytest.raises(LLMUnavailableError):
        llm.recommend(_ctx(), [])
    missing = LLMMapperService(client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="model not found")), base_url="http://x/v1"))
    with pytest.raises(LLMUnavailableError, match="404"):
        missing.recommend(_ctx(), [])


# ------------------------------------------------------------------ 3. end-to-end flow
def test_end_to_end_mapping_persists_draft_rules(service, session_factory, embeddings, legacy_table):
    llm, transport = make_llm()
    report = orchestrator(session_factory, embeddings, llm).generate_table_mapping_report(
        legacy_table.id, "BUSINESS_PARTNER", "AI_Draft_Mapping")

    # -- container: DRAFT, versioned, authored by the AI maker, no approver
    with session_factory() as s:
        mapping = s.get(MapTableRule, report.mapping_set_id)
        assert mapping.status == MappingStatus.DRAFT and mapping.version == 1
        assert mapping.created_by == "ai-mapper" and mapping.approved_by is None and mapping.approved_at is None
        assert mapping.source_table_id == legacy_table.id
        assert mapping.target_table_id == report.target_table_id
        rules = {
            (s.get(CatColumn, r.source_column_id).column_name): r
            for r in s.scalars(sa.select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping.id))
        }
        targets = {name: s.get(CatColumn, r.target_column_id) for name, r in rules.items()}
        target_tables = {name: c.table.table_name for name, c in targets.items()}

        # -- exactly the storable proposals were persisted
        assert set(rules) == {"CUST_ID", "CUST_NAME", "COUNTRY_CD", "POSTAL_CODE", "CUST_TYPE", "FAX_NO"}
        assert target_tables["CUST_NAME"] == "BUT000" and targets["CUST_NAME"].column_name == "NAME_ORG1"
        assert (target_tables["COUNTRY_CD"], targets["COUNTRY_CD"].column_name) == ("ADRC", "COUNTRY")
        # tie on rule count (3 BUT000 vs 3 ADRC) -> lowest table id, i.e. BUT000
        assert target_tables["CUST_NAME"] == "BUT000"
        assert s.get(MapTableRule, mapping.id).target_table.table_name == "BUT000"

        # -- confidence scores, AI metadata and rule types
        assert rules["CUST_NAME"].ai_confidence_score == Decimal("0.9300")
        assert rules["COUNTRY_CD"].ai_confidence_score == Decimal("0.9000")
        assert rules["FAX_NO"].ai_confidence_score == Decimal("0.4000")
        for r in rules.values():
            assert r.ai_suggested and r.ai_model_name == DEFAULT_MODEL
        assert rules["CUST_ID"].rule_type == RuleType.SQL_EXPRESSION
        assert rules["CUST_ID"].transformation_logic == "LPAD(TRIM(CUST_ID), 10, '0')"
        assert rules["COUNTRY_CD"].transformation_logic == "UPPER(TRIM(COUNTRY_CD))"
        assert rules["CUST_NAME"].rule_type == RuleType.DIRECT_COPY and rules["CUST_NAME"].transformation_logic is None
        assert rules["CUST_NAME"].is_mandatory_target is True  # NAME_ORG1 is mandatory
        assert rules["POSTAL_CODE"].is_mandatory_target is False

        # -- review flags live in ai_reasoning
        assert rules["CUST_NAME"].ai_reasoning == "because"
        assert rules["FAX_NO"].ai_reasoning.startswith("[NEEDS_REVIEW]") and "confidence 0.40" in rules["FAX_NO"].ai_reasoning
        assert rules["CUST_TYPE"].ai_reasoning.startswith("[NEEDS_REVIEW]") and "lookups" in rules["CUST_TYPE"].ai_reasoning

        # -- nothing unsafe was stored anywhere
        assert not s.scalars(sa.select(MapFieldRule).where(MapFieldRule.transformation_logic.ilike("%drop%"))).all()

    # -- report outcomes
    outcome = {p.source_column: p for p in report.proposals}
    assert {k: v.outcome for k, v in outcome.items()} == {
        "CUST_ID": ProposalOutcome.SAVED, "CUST_NAME": ProposalOutcome.SAVED,
        "CUSTOMER_TITLE": ProposalOutcome.SUPERSEDED, "COUNTRY_CD": ProposalOutcome.SAVED,
        "POSTAL_CODE": ProposalOutcome.SAVED, "CUST_TYPE": ProposalOutcome.SAVED,
        "EMAIL": ProposalOutcome.NO_MATCH, "LEGACY_NOTES": ProposalOutcome.NO_MATCH,
        "FAX_NO": ProposalOutcome.SAVED, "CREDIT_LIMIT": ProposalOutcome.REJECTED,
        "MISC_CODE": ProposalOutcome.REJECTED, "GARBAGE_COL": ProposalOutcome.ERROR,
    }
    assert outcome["CUST_NAME"].status == ReviewStatus.OK
    assert outcome["FAX_NO"].status == ReviewStatus.NEEDS_REVIEW
    assert any("not found in target catalog" in r for r in outcome["CREDIT_LIMIT"].reasons)
    assert any("semicolons" in r for r in outcome["MISC_CODE"].reasons)
    assert any("already claimed by CUST_NAME" in r for r in outcome["CUSTOMER_TITLE"].reasons)
    assert report.skipped_unprofiled_columns == ["UNPROFILED_COL"]
    assert "BUT000.BU_GROUP" in report.unmapped_mandatory_targets
    assert "BUT000.TYPE" not in report.unmapped_mandatory_targets

    # -- prompts: temperature 0, 5 candidates, <=3 masked samples, unprofiled column never sent
    asked = {json.loads(next(m["content"] for m in b["messages"] if m["role"] == "user"
                             and m["content"].startswith("{")))["source_column"]["name"] for b in transport.requests}
    assert "UNPROFILED_COL" not in asked and "CUST_NAME" in asked
    for body in transport.requests:
        assert body["temperature"] == 0.0 and body["model"] == DEFAULT_MODEL
    email_req = next(b for b in transport.requests if '"EMAIL"' in b["messages"][1]["content"])
    payload = json.loads(email_req["messages"][1]["content"])
    assert len(payload["candidates"]) == 5
    assert len(payload["source_column"]["sample_values_masked"]) == 3
    assert "john.doe" not in email_req["messages"][1]["content"]  # PII is masked before leaving the process


def test_generate_table_mapping_returns_id_and_versions_increment(service, session_factory, embeddings, legacy_table):
    orch = orchestrator(session_factory, embeddings, make_llm()[0])
    first = orch.generate_table_mapping(legacy_table.id)
    second = orch.generate_table_mapping(legacy_table.id, mapping_set_name="Second")
    assert isinstance(first, int) and second > first
    with session_factory() as s:
        assert s.get(MapTableRule, first).version == 1
        assert s.get(MapTableRule, second).version == 2
        assert s.get(MapTableRule, second).status == MappingStatus.DRAFT


def test_llm_outage_aborts_without_persisting(service, session_factory, embeddings, legacy_table):
    orch = orchestrator(session_factory, embeddings, make_llm(fail=httpx.ConnectError("down"))[0])
    with pytest.raises(LLMUnavailableError):
        orch.generate_table_mapping(legacy_table.id)
    with session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(MapTableRule)) == 0
        assert s.scalar(sa.select(sa.func.count()).select_from(MapFieldRule)) == 0


def test_orchestrator_input_validation(service, session_factory, embeddings, legacy_table):
    orch = orchestrator(session_factory, embeddings, make_llm()[0])
    with pytest.raises(NotFoundError):
        orch.generate_table_mapping(999_999)
    with pytest.raises(ValueError, match="configured for"):
        orch.generate_table_mapping(legacy_table.id, target_object_name="MATERIAL")

    target_table = service.get_target_dictionary().tables[0].table
    with pytest.raises(MappingValidationError, match="TARGET"):
        orch.generate_table_mapping(target_table.id)

    source = service.register_data_source(DataSourceCreate(
        system_name="OTHER", system_type=SystemType.FLAT_FILE, target_type=TargetType.SOURCE))
    bare = service.register_table(TableCreate(source_id=source.id, table_name="NEVER_PROFILED"))
    service.bulk_register_columns(BulkColumnRegistration(
        table_id=bare.id, columns=[ColumnCreate(column_name="A", data_type="VARCHAR")]))
    with pytest.raises(MappingValidationError, match="profiled"):
        orch.generate_table_mapping(bare.id)


def test_all_rejected_still_creates_empty_draft(service, session_factory, embeddings, legacy_table):
    script = {name: NO_MATCH_REPLY for name in SOURCE_COLUMNS}
    report = orchestrator(session_factory, embeddings, make_llm(script)[0]).generate_table_mapping_report(legacy_table.id)
    assert all(p.outcome == ProposalOutcome.NO_MATCH for p in report.proposals)
    with session_factory() as s:
        assert s.get(MapTableRule, report.mapping_set_id).status == MappingStatus.DRAFT
        assert s.scalar(sa.select(sa.func.count()).select_from(MapFieldRule)) == 0


# ------------------------------------------------------------------ optional live Ollama
def _ollama_has_model() -> bool:
    try:
        tags = httpx.get("http://localhost:11434/api/tags", timeout=2).json()
        return any(m["name"] == DEFAULT_MODEL for m in tags.get("models", []))
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_has_model(), reason=f"Ollama model {DEFAULT_MODEL} not installed")
def test_live_ollama_returns_contract_valid_recommendation(embeddings):
    llm = LLMMapperService()
    ctx = SourceColumnContext(column_id=1, column_name="COUNTRY_CD", data_type="VARCHAR2", char_max_length=2,
                              null_ratio=0.0, distinct_ratio=0.001, sample_values=["US", "DE", "FR"], table_name="AR_CUSTOMERS")
    result = llm.recommend(ctx, embeddings.get_top_k_candidates("COUNTRY_CD", "VARCHAR2", 5))
    assert result is None or isinstance(result, FieldMappingRecommendation)
