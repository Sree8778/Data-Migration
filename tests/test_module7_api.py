"""Module 7 tests: REST API lifecycle, security, error handling and the web UI shell.

The headline test drives the whole lifecycle through HTTP only:
upload -> profile -> AI mapping -> rule review -> transform/dedupe/validate -> approval ->
(blocked gate) -> clean re-run -> sign-off -> cockpit + SAP load -> reconciliation.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import warnings
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.context import AppContext
from src.api.main import create_app
from src.api.settings import Settings
from src.services.embedding_service import SentenceTransformerEncoder
from src.services.jira_service import JiraService
from src.services.llm_mapper_service import LLMMapperService
from src.services.lookup_service import COUNTRY_SEED, PAYMENT_TERMS_SEED
from tests.test_module4_transform_dedupe import HEADER, csv_path, dataset, env, expected_errors  # noqa: F401
from tests.test_module5_validation_governance import CONFIG as JIRA_CONFIG, FakeJira

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")

STEWARD, AUTHOR = "data.steward", "ai-mapper"


def U(name: str) -> dict:
    return {"X-User": name}


# ------------------------------------------------------------------ scripted LLM (OpenAI-compatible mock)
def rec(table, column, conf, rule="DIRECT_COPY", sql=None):
    return {"target_table": table, "target_column": column, "confidence_score": conf, "rule_type": rule,
            "transformation_sql": sql, "reasoning": f"{column} matches by meaning"}


NO_MATCH = rec("NO_MATCH", "NO_MATCH", 0)
SCRIPT = {
    "CUST_ID": rec("BUT000", "BP_EXT", 0.90, "SQL_EXPRESSION", "LPAD(TRIM(CUST_ID), 10, '0')"),
    "CUST_NAME": rec("BUT000", "NAME_ORG1", 0.95),
    "TAX_ID": NO_MATCH, "EMAIL": NO_MATCH, "PHONE": NO_MATCH,
    "ADDR_STREET": rec("ADRC", "STREET", 0.80, "SQL_EXPRESSION", "UPPER(TRIM(ADDR_STREET))"),
    "CITY": rec("ADRC", "CITY1", 0.92),
    "COUNTRY": rec("ADRC", "COUNTRY", 0.88, "VALUE_MAPPING"),
    "POSTAL_CODE": rec("ADRC", "POST_CODE1", 0.55),
    "PAYMENT_TERMS": rec("KNB1", "ZTERM", 0.70, "VALUE_MAPPING"),
}


def scripted_llm(fail: bool = False) -> LLMMapperService:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("ollama is down")
        body = json.loads(request.content)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user" and m["content"].startswith("{"))
        column = json.loads(user)["source_column"]["name"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(SCRIPT[column])}}]})
    return LLMMapperService(client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama.test/v1"))


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="session")
def encoder():
    enc = SentenceTransformerEncoder()
    try:
        enc.encode(["warm up"])
    except Exception as exc:  # no network / model unavailable
        pytest.skip(f"embedding model unavailable: {exc!r}")
    return enc


def make_ctx(session_factory, tmp_path, encoder=None, llm=None, jira=None, **settings) -> AppContext:
    cfg = Settings(workspace_dir=tmp_path / "workspace", auto_seed=False, **settings)
    return AppContext(cfg, session_factory, llm_mapper=llm or scripted_llm(), encoder=encoder, jira=jira)


def make_client(ctx: AppContext, user: str = "alice") -> TestClient:
    return TestClient(create_app(ctx), headers=U(user))


@pytest.fixture()
def ctx(service, session_factory, tmp_path, encoder):
    return make_ctx(session_factory, tmp_path, encoder)


@pytest.fixture()
def client(ctx):
    return make_client(ctx)


def subset(dataset, n=600, clean=False):
    rows = dataset[:n]
    return [r for r in rows if not expected_errors(r)] if clean else rows


def csv_bytes(rows) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


def upload(client, rows, table="AR_CUSTOMERS", system="LEGACY_ORACLE_AR"):
    return client.post("/api/sources/register", data={
        "system_name": system, "system_type": "ORACLE", "table_name": table, "primary_key_columns": "CUST_ID"},
        files={"file": ("ar_customers.csv", csv_bytes(rows), "text/csv")})


@pytest.fixture()
def api_env(env, ctx, dataset):
    """Module 4 catalog + mapping set (direct DB setup) with the dirty extract placed in the workspace."""
    directory = ctx.table_dir(env["table"].id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.csv").write_bytes(csv_bytes(subset(dataset)))
    return env


def rule_ids(detail) -> dict:
    return {f"{r['target_table']}.{r['target_column']}": r["rule_id"] for r in detail["rules"]}


# ------------------------------------------------------------------ the full lifecycle over HTTP
def test_full_lifecycle_upload_to_reconciliation(client, ctx, dataset):
    assert client.get("/api/health").json()["target_catalog_ready"] is False
    assert client.post("/api/admin/seed-sap-catalog").json()["tables"] == 6
    assert client.get("/api/health").json()["target_catalog_ready"] is True

    # ---- upload / register
    reg = upload(client, subset(dataset))
    assert reg.status_code == 201, reg.text
    body = reg.json()
    table_id = body["table_id"]
    assert body["columns_registered"] == len(HEADER) and body["bytes_stored"] > 0
    listed = client.get("/api/tables").json()
    assert [t["table_name"] for t in listed] == ["AR_CUSTOMERS"] and listed[0]["has_source_file"]

    # ---- profile
    profile = client.post(f"/api/profile/{table_id}")
    assert profile.status_code == 200, profile.text
    p = profile.json()
    assert p["total_rows"] == len(subset(dataset)) and p["column_count"] == len(HEADER)
    cols = {c["column_name"]: c for c in p["columns"]}
    assert cols["CUST_ID"]["health_band"] == "high" and "NEAR_UNIQUE_KEY" in cols["CUST_ID"]["flags"]
    assert cols["CITY"]["null_count"] > 0 and cols["CITY"]["health_score"] < 100
    assert all(0 <= c["health_score"] <= 100 for c in p["columns"]) and ["CUST_ID"] in p["inferred_keys"]
    assert client.get(f"/api/profile/{table_id}").json()["total_rows"] == p["total_rows"]

    # ---- AI mapping (scripted local LLM)
    gen = client.post(f"/api/mapping/generate/{table_id}")
    assert gen.status_code == 201, gen.text
    g = gen.json()
    mapping_id = g["mapping_set_id"]
    outcomes = {pr["source_column"]: pr["outcome"] for pr in g["report"]["proposals"]}
    assert outcomes["TAX_ID"] == "NO_MATCH" and outcomes["CUST_NAME"] == "SAVED"
    detail = g["mapping"]
    assert detail["mapping"]["status"] == "DRAFT" and detail["mapping"]["rule_count"] == 7
    by_target = {f"{r['target_table']}.{r['target_column']}": r for r in detail["rules"]}
    assert by_target["BUT000.NAME_ORG1"]["confidence_band"] == "high" and by_target["BUT000.NAME_ORG1"]["ai_suggested"]
    assert by_target["ADRC.POST_CODE1"]["confidence_band"] == "low" and by_target["ADRC.POST_CODE1"]["needs_review"]
    assert by_target["ADRC.COUNTRY"]["needs_review"] and "lookups" in " ".join(by_target["ADRC.COUNTRY"]["review_reasons"])
    assert {"BUT000.BU_GROUP", "BUT000.TYPE"} <= set(detail["unmapped_mandatory_targets"])
    assert any(c["column_name"] == "TAX_ID" and not c["mapped"] for c in detail["source_columns"])
    rid = rule_ids(detail)

    # ---- human review: lookups, confirm, adjust, add static defaults
    country = client.put(f"/api/mapping/rules/{rid['ADRC.COUNTRY']}", json={"lookups": COUNTRY_SEED, "confirm": True})
    assert country.status_code == 200, country.text
    assert country.json()["lookup_count"] == len(COUNTRY_SEED) and not country.json()["needs_review"]
    assert country.json()["reviewed_by"] == "alice" and country.json()["confidence"] == 0.88  # AI provenance kept
    assert client.put(f"/api/mapping/rules/{rid['KNB1.ZTERM']}", json={"lookups": PAYMENT_TERMS_SEED, "confirm": True}).status_code == 200
    post = client.put(f"/api/mapping/rules/{rid['ADRC.POST_CODE1']}", json={"confirm": True, "note": "format verified"})
    assert post.json()["reasoning"].endswith("Note (alice): format verified") and not post.json()["needs_review"]
    adjusted = client.put(f"/api/mapping/rules/{rid['ADRC.STREET']}", json={"transformation_logic": "UPPER(TRIM(ADDR_STREET))", "confirm": True})
    assert adjusted.json()["transformation_logic"] == "UPPER(TRIM(ADDR_STREET))"
    for column, logic in (("BU_GROUP", "'BP01'"), ("TYPE", "'2'")):
        created = client.post(f"/api/mapping/{mapping_id}/rules", json={
            "target_table": "BUT000", "target_column": column, "rule_type": "STATIC_VALUE", "transformation_logic": logic})
        assert created.status_code == 201 and not created.json()["ai_suggested"] and created.json()["target_mandatory"]
    still = client.get(f"/api/mapping/{mapping_id}").json()["unmapped_mandatory_targets"]
    assert still == ["KNB1.AKONT", "KNVV.WAERS"]  # the static defaults now cover BU_GROUP and TYPE

    # ---- transform + dedupe + validate on the dirty extract
    run1 = client.post(f"/api/pipeline/run/{mapping_id}", json={"wave_name": "WAVE_1"})
    assert run1.status_code == 201, run1.text
    r1 = run1.json()
    assert r1["dedup_applied"] and r1["validation"]["evaluated_records"] > 0
    assert r1["run"]["source_records"] == len(subset(dataset)) and r1["run"]["status"] == "SUCCEEDED"
    assert r1["defects"]["total_open"] > 0 and r1["readiness"]["state"] == "BLOCKED"
    run1_id = r1["run_id"]

    defects = client.get(f"/api/defects/{run1_id}").json()
    assert defects["open_critical"] > 0 and defects["jira_mode"] == "dry-run"
    assert defects["defects"][0]["severity"] == "CRITICAL" and defects["defects"][0]["root_cause"]
    assert all(d["jira_issue_key"] is None for d in defects["defects"])
    sync = client.post(f"/api/defects/{run1_id}/sync-jira").json()
    assert sync["dry_run"] and all(k.startswith("DRY-RUN:") for k in sync["issue_keys"]) and sync["issue_keys"]

    # ---- approval with segregation of duties
    assert client.post(f"/api/governance/approve-mapping/{mapping_id}", headers=U(STEWARD)).status_code == 409  # still DRAFT
    assert client.post(f"/api/governance/submit-mapping/{mapping_id}").json()["status"] == "SUBMITTED"
    self_approval = client.post(f"/api/governance/approve-mapping/{mapping_id}", headers=U(AUTHOR))
    assert self_approval.status_code == 403 and self_approval.json()["code"] == "SEGREGATION_OF_DUTIES"
    approved = client.post(f"/api/governance/approve-mapping/{mapping_id}", headers=U(STEWARD))
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED" and approved.json()["approved_by"] == STEWARD
    frozen = client.put(f"/api/mapping/rules/{rid['ADRC.CITY1']}", json={"confirm": True})
    assert frozen.status_code == 409 and "only DRAFT" in frozen.json()["detail"]

    # ---- the gate blocks while CRITICAL defects remain
    blocked = client.post(f"/api/governance/sign-off-load/{run1_id}", headers=U(STEWARD))
    assert blocked.status_code == 409 and blocked.json()["code"] == "GATE_BLOCKED"
    assert any("CRITICAL" in r for r in blocked.json()["detail"]["reasons"])
    assert client.post(f"/api/pipeline/load/{run1_id}", json={"mode": "cockpit"}).status_code == 409

    # ---- stewards fix the extract: re-upload clean data and re-run
    assert upload(client, subset(dataset, clean=True)).status_code == 201
    run2 = client.post(f"/api/pipeline/run/{mapping_id}", json={"wave_name": "WAVE_1_FIX"}).json()
    run_id = run2["run_id"]
    assert run2["defects"]["total_open"] == 0 and run2["readiness"]["state"] == "READY_FOR_LOAD"
    assert run2["validation"]["quality_score"] == 100.0

    # ---- sign-off (SoD), then load: cockpit workbook + SAP sandbox
    assert client.post(f"/api/pipeline/load/{run_id}", json={"mode": "cockpit"}).status_code == 409  # not signed off yet
    sod = client.post(f"/api/governance/sign-off-load/{run_id}", headers=U(AUTHOR))
    assert sod.status_code == 403 and sod.json()["code"] == "SEGREGATION_OF_DUTIES"
    signed = client.post(f"/api/governance/sign-off-load/{run_id}", headers=U(STEWARD))
    assert signed.status_code == 200 and signed.json()["signed_off_by"] == STEWARD and signed.json()["readiness"]["state"] == "READY_FOR_LOAD"

    cockpit = client.post(f"/api/pipeline/load/{run_id}", json={"mode": "cockpit"})
    assert cockpit.status_code == 200, cockpit.text
    assert cockpit.json()["cockpit"]["business_partners"] == run2["validation"]["valid_records"]
    download = client.get(cockpit.json()["download_url"])
    assert download.status_code == 200 and zipfile.is_zipfile(io.BytesIO(download.content))
    assert "migration_cockpit_run_" in download.headers["content-disposition"]

    loaded = client.post(f"/api/pipeline/load/{run_id}", json={"mode": "sap", "target": "sandbox", "batch_size": 100})
    assert loaded.status_code == 200, loaded.text
    lo = loaded.json()
    assert lo["simulated"] and lo["load"]["loaded"] == run2["validation"]["valid_records"] and lo["load"]["failed"] == 0
    again = client.post(f"/api/pipeline/load/{run_id}", json={"mode": "sap"})
    assert again.status_code == 409 and again.json()["code"] == "LOAD_STATE"  # a run loads once

    # ---- reconciliation certificate
    recon = client.get(f"/api/reconciliation/{run_id}")
    assert recon.status_code == 200, recon.text
    rj = recon.json()
    assert rj["passed"] and rj["report"]["balanced"] and rj["report"]["variance"] == 0 and rj["report"]["complete"]
    assert rj["report"]["counts"]["loaded"] == lo["load"]["loaded"] and rj["cockpit_download_url"]
    md = client.get(rj["export_urls"]["markdown"])
    assert md.status_code == 200 and "Result: PASSED" in md.text
    assert "<table>" in client.get(rj["export_urls"]["html"]).text

    runs = {r["run_id"]: r for r in client.get("/api/runs").json()}
    assert runs[run_id]["phase"] == "LOAD" and runs[run_id]["records_loaded"] == lo["load"]["loaded"]
    assert runs[run1_id]["phase"] == "VALIDATE" and runs[run_id]["quality_score"] == 100.0
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "SUCCEEDED"


# ------------------------------------------------------------------ pipeline / governance behaviours
def test_pipeline_defects_and_live_jira_sync(api_env, session_factory, tmp_path, encoder):
    fake = FakeJira()
    jira = JiraService(session_factory, config=JIRA_CONFIG, transport=httpx.MockTransport(fake), retry_delay=0)
    client = make_client(make_ctx(session_factory, tmp_path, encoder, jira=jira))
    run = client.post(f"/api/pipeline/run/{api_env['mapping'].id}", json={}).json()
    run_id = run["run_id"]

    assert client.get(f"/api/defects/{run_id}").json()["jira_mode"] == "live"
    sync = client.post(f"/api/defects/{run_id}/sync-jira").json()
    assert not sync["dry_run"] and sync["errors"] == [] and len(sync["issue_keys"]) == run["defects"]["total_open"]
    assert all(re.fullmatch(r"MIG-\d+", k) for k in sync["issue_keys"])
    after = client.get(f"/api/defects/{run_id}").json()
    assert {d["jira_issue_key"] for d in after["defects"]} == set(sync["issue_keys"])
    assert {d["jira_status"] for d in after["defects"]} == {"OPEN"} and len(fake.requests) == len(sync["issue_keys"])


def test_pipeline_options_and_input_errors(api_env, client, ctx):
    mid = api_env["mapping"].id
    ok = client.post(f"/api/pipeline/run/{mid}", json={"deduplicate": False, "lineage": "NONE"})
    assert ok.status_code == 201 and ok.json()["dedup_applied"] is False and ok.json()["run"]["lineage_rows"] == 0

    assert client.post("/api/pipeline/run/999999", json={}).status_code == 404
    shutil.rmtree(ctx.table_dir(api_env["table"].id))
    missing = client.post(f"/api/pipeline/run/{mid}", json={})
    assert missing.status_code == 409 and "no source file" in missing.json()["detail"]
    assert client.post("/api/profile/999999").status_code == 404
    assert client.post(f"/api/profile/{api_env['table'].id}").status_code == 409


def test_load_requires_a_validated_run_and_known_target(api_env, client):
    assert client.post("/api/pipeline/load/12345", json={"mode": "cockpit"}).status_code == 409  # nothing validated
    run_id = client.post(f"/api/pipeline/run/{api_env['mapping'].id}", json={}).json()["run_id"]
    assert client.post(f"/api/pipeline/load/{run_id}", json={"mode": "bogus"}).status_code == 422
    assert client.post(f"/api/pipeline/load/{run_id}", json={"mode": "sap", "target": "live"}).status_code == 409  # not signed off
    assert client.get("/api/defects/999999").status_code == 404 and client.get("/api/reconciliation/999999").status_code == 404
    assert client.get(f"/api/runs/{run_id}/cockpit").status_code == 404


def test_live_sap_target_requires_configuration(api_env, session_factory, tmp_path, encoder, monkeypatch):
    for var in ("SAP_BASE_URL", "SAP_USER", "SAP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    client = make_client(make_ctx(session_factory, tmp_path, encoder))
    mid = api_env["mapping"].id
    client.post(f"/api/governance/submit-mapping/{mid}")
    client.post(f"/api/governance/approve-mapping/{mid}", headers=U(STEWARD))
    # clean data -> gate opens; then 'live' without SAP_* env is a configuration error, not a crash
    directory = tmp_path / "workspace" / "tables" / str(api_env["table"].id)
    rows = [r for r in csv.DictReader(io.StringIO((directory / "source.csv").read_text())) if not expected_errors(r)]
    (directory / "source.csv").write_bytes(csv_bytes(rows))
    run_id = client.post(f"/api/pipeline/run/{mid}", json={}).json()["run_id"]
    assert client.post(f"/api/governance/sign-off-load/{run_id}", headers=U(STEWARD)).status_code == 200
    live = client.post(f"/api/pipeline/load/{run_id}", json={"mode": "sap", "target": "live"})
    assert live.status_code == 400 and live.json()["code"] == "NOT_CONFIGURED"


# ------------------------------------------------------------------ rule editing guards
def test_rule_edits_are_validated_and_guarded(api_env, client, session_factory):
    mid = api_env["mapping"].id
    detail = client.get(f"/api/mapping/{mid}").json()
    rid = rule_ids(detail)
    base = f"/api/mapping/rules/{rid['BUT000.BP_EXT']}"

    for bad in ("UPPER(CUST_ID); DROP TABLE cat_columns", "pg_sleep(5)", "(SELECT 1)", "nope_col"):
        r = client.put(base, json={"rule_type": "SQL_EXPRESSION", "transformation_logic": bad})
        assert r.status_code == 422 and "guardrails" in r.json()["detail"], bad
    assert client.put(base, json={"rule_type": "SQL_EXPRESSION"}).status_code in (200, 422)  # keeps its own logic
    assert client.put(base, json={"rule_type": "DIRECT_COPY", "transformation_logic": "TRIM(CUST_ID)"}).status_code == 422
    assert client.put(base, json={"target_table": "BUT000"}).status_code == 422  # table without column
    assert client.put(base, json={"target_table": "BUT000", "target_column": "NOPE"}).status_code == 404
    assert client.put(base, json={"lookups": {"a": "b"}}).status_code == 422  # lookups need VALUE_MAPPING
    assert client.put(base, json={"nonsense": 1}).status_code == 422  # unknown field
    assert client.put("/api/mapping/rules/999999", json={"confirm": True}).status_code == 404

    clash = client.put(base, json={"target_table": "BUT000", "target_column": "NAME_ORG1"})
    assert clash.status_code == 409 and clash.json()["code"] == "CONFLICT"  # one rule per target column

    switched = client.put(base, json={"rule_type": "DIRECT_COPY"})
    assert switched.status_code == 200 and switched.json()["transformation_logic"] is None
    assert "[ADJUSTED by alice]" in switched.json()["reasoning"] and switched.json()["rule_type"] == "DIRECT_COPY"
    retargeted = client.put(base, json={"target_table": "bUT000", "target_column": "name_org2", "confirm": True})
    assert retargeted.status_code == 200 and retargeted.json()["target_column"] == "NAME_ORG2"  # case-insensitive lookup

    dup = client.post(f"/api/mapping/{mid}/rules", json={"target_table": "BUT000", "target_column": "BU_SORT1", "rule_type": "DIRECT_COPY", "source_column": "CUST_NAME"})  # already mapped
    assert dup.status_code == 409
    assert client.post(f"/api/mapping/{mid}/rules", json={"target_table": "BUT000", "target_column": "BU_GROUP", "rule_type": "SQL_EXPRESSION"}).status_code == 422
    assert client.post(f"/api/mapping/{mid}/rules", json={"target_table": "BUT000", "target_column": "BU_GROUP", "rule_type": "DIRECT_COPY"}).status_code == 422
    assert client.post("/api/mapping/999999/rules", json={"target_table": "A", "target_column": "B", "rule_type": "STATIC_VALUE", "transformation_logic": "'x'"}).status_code == 404


def test_mapping_generation_reports_llm_outage(service, session_factory, tmp_path, encoder, dataset):
    client = make_client(make_ctx(session_factory, tmp_path, encoder, llm=scripted_llm(fail=True)))
    client.post("/api/admin/seed-sap-catalog")
    table_id = upload(client, subset(dataset)).json()["table_id"]
    client.post(f"/api/profile/{table_id}")
    r = client.post(f"/api/mapping/generate/{table_id}")
    assert r.status_code == 503 and r.json()["code"] == "LLM_UNAVAILABLE"
    assert client.get("/api/mapping-sets").json() == []  # nothing half-created


# ------------------------------------------------------------------ validation, status codes, CORS, auth
def test_request_validation_errors_are_clean(client):
    r = client.post("/api/sources/register", data={"system_type": "ORACLE"})  # system_name missing
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert all(set(e) == {"loc", "msg", "type"} for e in r.json()["detail"])  # request input is never echoed back
    assert client.post("/api/sources/register", data={"system_name": "X", "system_type": "NOT_A_SYSTEM"}).status_code == 422

    assert client.get("/api/mapping/not-a-number").status_code == 422
    assert client.post("/api/pipeline/run/1", json={"wave_name": ""}).status_code == 422
    assert client.post("/api/pipeline/run/1", json={"unknown": True}).status_code == 422
    assert client.post("/api/pipeline/load/1", json={"batch_size": 0}).status_code == 422
    assert client.get("/api/does-not-exist").json()["code"] == "HTTP_404"
    assert client.delete("/api/runs").status_code == 405


def test_source_registration_rules(client, ctx):
    only_source = client.post("/api/sources/register", data={"system_name": "ORA_PROD", "system_type": "ORACLE"})
    assert only_source.status_code == 201 and only_source.json()["table_id"] is None

    no_table = client.post("/api/sources/register", data={"system_name": "X"}, files={"file": ("a.csv", b"A\n1\n", "text/csv")})
    assert no_table.status_code == 422 and "table_name" in no_table.json()["detail"]
    wrong = client.post("/api/sources/register", data={"system_name": "X", "table_name": "T"}, files={"file": ("a.xlsx", b"x", "application/octet-stream")})
    assert wrong.status_code == 422 and "unsupported file type" in wrong.json()["detail"]
    empty = client.post("/api/sources/register", data={"system_name": "X", "table_name": "T"}, files={"file": ("a.csv", b"", "text/csv")})
    assert empty.status_code == 422
    bad_pk = client.post("/api/sources/register", data={"system_name": "X", "table_name": "T", "primary_key_columns": "NOPE"},
                         files={"file": ("a.csv", b"A,B\n1,2\n", "text/csv")})
    assert bad_pk.status_code == 422 and "primary_key_columns" in bad_pk.json()["detail"]
    bad_cfg = client.post("/api/sources/register", data={"system_name": "X", "connection_config": "[1]"})
    assert bad_cfg.status_code == 422
    secret = client.post("/api/sources/register", data={"system_name": "X", "connection_config": '{"password": "hunter2"}'})
    assert secret.status_code == 422  # the catalog refuses plaintext secrets

    ok = client.post("/api/sources/register", data={"system_name": "X", "table_name": "T", "primary_key_columns": "a"},
                     files={"file": ("a.csv", b"A,B\n1,2\n3,4\n", "text/csv")})
    assert ok.status_code == 201 and ok.json()["columns_registered"] == 2
    again = client.post("/api/sources/register", data={"system_name": "X", "table_name": "T"},
                        files={"file": ("a.csv", b"A,B,C\n1,2,3\n", "text/csv")})
    assert again.json()["table_id"] == ok.json()["table_id"] and again.json()["columns_registered"] == 3  # idempotent
    assert client.get("/api/tables").json()[-1]["column_count"] == 3


def test_upload_size_limit(service, session_factory, tmp_path):
    client = make_client(make_ctx(session_factory, tmp_path, max_upload_mb=1))
    big = b"A,B\n" + b"x,y\n" * 400_000  # ~1.6 MB
    r = client.post("/api/sources/register", data={"system_name": "X", "table_name": "T"}, files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413
    assert not list((tmp_path / "workspace").rglob("source.*")) and not list((tmp_path / "workspace").rglob("*.part"))


def test_cors_allows_only_configured_origins(service, session_factory, tmp_path):
    client = make_client(make_ctx(session_factory, tmp_path, cors_origins=("https://migration.example.com",)))
    ok = client.options("/api/runs", headers={"Origin": "https://migration.example.com", "Access-Control-Request-Method": "GET",
                                              "Access-Control-Request-Headers": "x-user"})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://migration.example.com"
    assert "access-control-allow-credentials" not in ok.headers and "x-user" in ok.headers["access-control-allow-headers"].lower()
    assert "PUT" in ok.headers["access-control-allow-methods"] and "DELETE" not in ok.headers["access-control-allow-methods"]

    evil = client.options("/api/runs", headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers and evil.status_code == 400
    simple = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert simple.status_code == 200 and "access-control-allow-origin" not in simple.headers
    assert client.get("/api/health", headers={"Origin": "https://migration.example.com"}).headers["access-control-allow-origin"] == "https://migration.example.com"


def test_identity_is_required_for_changes_and_api_keys_are_enforced(service, session_factory, tmp_path):
    dev = TestClient(create_app(make_ctx(session_factory, tmp_path)))  # dev mode, no X-User
    assert dev.get("/api/runs").status_code == 200
    anonymous = dev.post("/api/admin/seed-sap-catalog")
    assert anonymous.status_code == 401 and anonymous.json()["code"] == "HTTP_401"
    assert dev.get("/api/health").json()["auth_mode"] == "dev-header"

    secured = TestClient(create_app(make_ctx(session_factory, tmp_path, api_keys={"k-alice": "alice", "k-bob": STEWARD})))
    assert secured.get("/api/health").json()["auth_mode"] == "api-key"  # probes stay open
    assert secured.get("/api/runs").status_code == 401
    assert secured.get("/api/runs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert secured.get("/api/runs", headers={"X-API-Key": "k-alice"}).status_code == 200
    # the key decides the identity; a spoofed X-User is ignored
    seeded = secured.post("/api/admin/seed-sap-catalog", headers={"X-API-Key": "k-alice", "X-User": STEWARD})
    assert seeded.status_code == 200


def test_ui_pages_serve_the_enterprise_light_theme(client):
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    for token in ("#F8FAFC", "#0F172A", "--green-bg", "--amber-bg", "--red-bg", "--purple-bg", "nth-child(even)"):
        assert token in css.text, token

    pages = {"/": "Workspace", "/ui/profiler/7": "Table Profiler", "/ui/studio/7": "Mapping Studio",
             "/ui/governance/3": "Defect &amp; Governance Hub", "/ui/certificate/3": "Reconciliation Certificate"}
    for path, title in pages.items():
        r = client.get(path)
        assert r.status_code == 200 and title in r.text and "/static/js/common.js" in r.text, path
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text), "inline script would violate the CSP"
    assert 'data-table-id="7"' in client.get("/ui/studio/7").text and 'data-run-id="3"' in client.get("/ui/governance/3").text
    for script in ("common", "index", "profiler", "studio", "governance", "certificate"):
        js = client.get(f"/static/js/{script}.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    studio = client.get("/static/js/studio.js").text
    assert "AI-suggested" in studio and "Needs review" in studio  # the semantic badges the design calls for
    assert "badge(\"purple\"" in studio.replace("'", '"') or "purple" in studio
    assert client.get("/docs").status_code == 200  # interactive API docs stay available


def test_openapi_exposes_the_lifecycle_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        ("post", "/api/sources/register"), ("post", "/api/profile/{table_id}"),
        ("post", "/api/mapping/generate/{source_table_id}"), ("put", "/api/mapping/rules/{rule_id}"),
        ("post", "/api/governance/approve-mapping/{mapping_set_id}"), ("post", "/api/pipeline/run/{mapping_set_id}"),
        ("get", "/api/defects/{run_id}"), ("post", "/api/governance/sign-off-load/{run_id}"),
        ("post", "/api/pipeline/load/{run_id}"), ("get", "/api/reconciliation/{run_id}"),
    }
    assert expected <= {(m, p) for p, ops in paths.items() for m in ops}
