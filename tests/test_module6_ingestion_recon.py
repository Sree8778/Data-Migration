"""Module 6 tests: cockpit workbook, gated SAP loading (sandbox + OData adapter), reconciliation.

End-to-end fixtures run the real Module 4 -> 5 pipeline on the dirty customer dataset (a 600-row
slice for speed, the full 5,000+ rows for the headline test), approve the mapping through the
governance service, clear the defects, and then load into the SAP sandbox simulator.
"""

from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal  # noqa: F401

import duckdb
import httpx
import pandas as pd
import pytest
import sqlalchemy as sa

from src.db.enums import MigrationPhase, RunStatus
from src.db.models import MigCellLineage, MigMigrationRun
from src.schemas.sap import BapiRet2
from src.services.bp_model import BpConfig, alpha_input, iter_bp_records
from src.services.cockpit_generator import (
    ADDRESS,
    COMPANY,
    GENERAL,
    ROLES,
    SALES,
    SHEET_ORDER,
    CockpitConsistencyError,
    CockpitGenerator,
    column_letter,
    iter_sheet_rows,
    list_sheets,
    validate_workbook,
)
from src.services.deduplication_engine import DeduplicationEngine
from src.services.defect_service import DefectService
from src.services.execution_orchestrator import ExecutionOrchestrator
from src.services.governance_service import GateBlockedError, GovernanceService
from src.services.metadata_service import NotFoundError
from src.services.reconciliation_service import ReconciliationService, render_html, render_markdown
from src.services.sap_client import (
    HttpSapClient,
    SandboxSapClient,
    SapAuthError,
    SapConfig,
    SapUnavailableError,
    build_odata_payload,
)
from src.services.sap_loader_service import (
    PARTNER_COLUMN,
    LoadInputError,
    LoadStateError,
    SapLoaderService,
)
from src.services.transformation_engine import TransformationEngine
from src.services.validation_engine import ValidationEngine
from tests.test_module4_transform_dedupe import (  # noqa: F401  (fixtures + helpers from Module 4)
    DEDUP,
    HEADER,
    PASSTHROUGH,
    csv_path,
    dataset,
    env,
    read_parquet,
)
from tests.test_module5_validation_governance import (  # noqa: F401
    GOOD,
    make_run,
    submit_and_approve,
    write_staging,
)


# ------------------------------------------------------------------ pipeline helpers
@dataclass
class Pipe:
    run: object  # RunReport
    report: object  # first ValidationReport (full accounting)
    valid_path: str
    gov: GovernanceService
    env: dict


def run_pipeline(env, sf, tmp_path, csv_file) -> Pipe:
    run = ExecutionOrchestrator(sf, TransformationEngine(sf), DeduplicationEngine(), output_dir=tmp_path / "out").execute_run(
        env["mapping"].id, csv_file, "WAVE_1", DEDUP, passthrough_columns=PASSTHROUGH)
    report = ValidationEngine(sf).validate(env["mapping"].id, run.deduplicated_path, tmp_path / "val1", run_id=run.run_id)
    DefectService(sf).record_defects(run.run_id, report)
    return Pipe(run, report, report.valid_path, GovernanceService(sf), env)


def make_loadable(pipe: Pipe, sf, tmp_path) -> Pipe:
    """Approve the mapping and clear the defects by re-validating the valid payload."""
    submit_and_approve(pipe.gov, pipe.env["mapping"].id)
    clean = ValidationEngine(sf).validate(pipe.env["mapping"].id, pipe.report.valid_path, tmp_path / "val2")
    DefectService(sf).record_defects(pipe.run.run_id, clean)
    assert pipe.gov.evaluate_readiness(pipe.run.run_id).ready
    return pipe


def subset_csv(dataset, tmp_path, n=600):
    path = tmp_path / "subset.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(dataset[:n])
    return path


@pytest.fixture()
def pipe(env, session_factory, dataset, tmp_path):
    return run_pipeline(env, session_factory, tmp_path, subset_csv(dataset, tmp_path))


@pytest.fixture()
def ready(pipe, session_factory, tmp_path):
    return make_loadable(pipe, session_factory, tmp_path)


def loader(sf, client, gov=None, batch_size=100):
    return SapLoaderService(sf, gov or GovernanceService(sf), client, batch_size=batch_size)


def lineage(sf, run_id, **filters):
    with sf() as s:
        q = sa.select(MigCellLineage).where(MigCellLineage.run_id == run_id)
        for k, v in filters.items():
            q = q.where(getattr(MigCellLineage, k) == v)
        return s.scalars(q).all()


def run_row(sf, run_id) -> MigMigrationRun:
    with sf() as s:
        return s.get(MigMigrationRun, run_id)


def write_full_staging(path, rows: list[dict]):
    frame = pd.DataFrame(rows)
    con = duckdb.connect()
    con.register("frame", frame)
    con.execute(f"COPY (SELECT * FROM frame) TO '{path.as_posix()}' (FORMAT parquet)")
    return path


FULL = {
    "_SRC_PK": "1", "BUT000_BP_EXT": "123", "BUT000_TYPE": "2", "BUT000_BU_GROUP": "BP01",
    "BUT000_NAME_ORG1": "Acme & Sons <GmbH>", "BUT000_NAME_ORG2": None, "BUT000_BU_SORT1": "ACME",
    "ADRC_COUNTRY": "DE", "ADRC_STREET": "Hauptstr.", "ADRC_HOUSE_NUM1": "7", "ADRC_CITY1": "Berlin",
    "ADRC_POST_CODE1": "01067", "ADRC_REGION": "BE", "ADRC_LANGU": "D",
    "KNB1_BUKRS": "1000", "KNB1_AKONT": "113100", "KNB1_ZTERM": "NT30", "KNB1_ZWELS": "CT",
    "KNVV_VKORG": "1010", "KNVV_VTWEG": "10", "KNVV_SPART": "00", "KNVV_WAERS": "EUR", "KNVV_INCO1": "EXW",
}


# ------------------------------------------------------------------ ALPHA / helpers
def test_alpha_conversion_exit():
    assert alpha_input("123") == "0000000123"
    assert alpha_input("  45 ") == "0000000045"
    assert alpha_input("0000000123") == "0000000123"
    assert alpha_input("ABC-1") == "ABC-1"  # not purely numeric: unchanged
    assert alpha_input("12345678901") == "12345678901"  # never truncated
    assert alpha_input("113100", 10) == "0000113100"
    assert alpha_input("   ") is None and alpha_input(None) is None


def test_column_letters_and_bp_config():
    assert [column_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]
    with pytest.raises(ValueError):
        BpConfig(default_roles=("FLCU00", "NOPE"))


# ------------------------------------------------------------------ 1. cockpit workbook
def test_cockpit_workbook_has_five_consistent_sheets(tmp_path):
    staging = write_full_staging(tmp_path / "valid.parquet", [
        FULL,
        {**FULL, "_SRC_PK": "2", "BUT000_BP_EXT": "  45 ", "KNB1_AKONT": "7", "BUT000_NAME_ORG2": "Division <2>",
         "BUT000_NAME_ORG1": "Control\x01Char"},
        {**FULL, "_SRC_PK": "3", "BUT000_BP_EXT": "ABC-1", "ADRC_COUNTRY": None, "ADRC_STREET": None, "ADRC_HOUSE_NUM1": None,
         "ADRC_CITY1": None, "ADRC_POST_CODE1": None, "ADRC_REGION": None, "ADRC_LANGU": None,
         "KNB1_BUKRS": None, "KNB1_AKONT": None, "KNB1_ZTERM": None, "KNB1_ZWELS": None,
         "KNVV_VKORG": None, "KNVV_VTWEG": None, "KNVV_SPART": None, "KNVV_WAERS": None, "KNVV_INCO1": None},
    ])
    result = CockpitGenerator().generate(staging, tmp_path / "cockpit.xlsx")
    path = result.path
    assert zipfile.is_zipfile(path) and list_sheets(path) == SHEET_ORDER
    assert [n.split(" (")[1].rstrip(")") for n in SHEET_ORDER] == ["BUT000", "BUT100", "ADRC", "KNB1", "KNVV"]
    assert result.business_partners == 3
    assert result.sheet_rows == {GENERAL: 3, ROLES: 6, ADDRESS: 2, COMPANY: 2, SALES: 2}
    assert validate_workbook(path) == []

    general = list(iter_sheet_rows(path, GENERAL))
    assert general[0] == ["BP_EXT", "TYPE", "BU_GROUP", "NAME_ORG1", "NAME_ORG2", "BU_SORT1"]
    keys = [r[0] for r in general[1:]]
    assert keys == ["0000000123", "0000000045", "ABC-1"]  # ALPHA conversion on the join key
    assert general[1][3] == "Acme & Sons <GmbH>"  # XML escaping round-trips
    assert general[2][3] == "ControlChar"  # illegal XML control characters stripped
    assert general[2][4] == "Division <2>"

    roles = list(iter_sheet_rows(path, ROLES))[1:]
    assert sorted(roles) == sorted([[k, r] for k in keys for r in ("FLCU00", "FLCU01")])
    company = list(iter_sheet_rows(path, COMPANY))
    assert company[0] == ["BP_EXT", "BUKRS", "AKONT", "ZTERM", "ZWELS"]
    assert company[1] == ["0000000123", "1000", "0000113100", "NT30", "CT"]  # ALPHA on AKONT, text keeps zeros
    assert company[2][2] == "0000000007"
    address = list(iter_sheet_rows(path, ADDRESS))
    assert address[1][5] == "01067"  # leading zero preserved as text
    assert list(iter_sheet_rows(path, SALES))[1] == ["0000000123", "1010", "10", "00", "EUR", "EXW"]

    # every child key exists on the General Data sheet (referential consistency)
    for sheet in (ROLES, ADDRESS, COMPANY, SALES):
        assert {r[0] for r in list(iter_sheet_rows(path, sheet))[1:]} <= set(keys)


def test_cockpit_roles_from_staging_column_and_defaults(tmp_path):
    staging = write_full_staging(tmp_path / "v.parquet", [
        {**FULL, "BUT100_RLTYP": "FLVN00, FLVN01"}, {**FULL, "_SRC_PK": "2", "BUT000_BP_EXT": "2", "BUT100_RLTYP": "FLCU00"}])
    result = CockpitGenerator().generate(staging, tmp_path / "w.xlsx")
    roles = list(iter_sheet_rows(result.path, ROLES))[1:]
    assert sorted(roles) == [["0000000002", "FLCU00"], ["0000000123", "FLVN00"], ["0000000123", "FLVN01"]]

    only_vendor = CockpitGenerator(BpConfig(default_roles=("FLVN00",))).generate(
        write_full_staging(tmp_path / "v2.parquet", [FULL]), tmp_path / "w2.xlsx")
    assert list(iter_sheet_rows(only_vendor.path, ROLES))[1:] == [["0000000123", "FLVN00"]]

    bad = write_full_staging(tmp_path / "v3.parquet", [{**FULL, "BUT100_RLTYP": "XXXX"}])
    with pytest.raises(CockpitConsistencyError, match="unsupported BP role"):
        CockpitGenerator().generate(bad, tmp_path / "w3.xlsx")


def test_cockpit_refuses_inconsistent_input_and_leaves_no_file(tmp_path):
    cases = {
        "dup": ([FULL, {**FULL, "_SRC_PK": "2", "BUT000_BP_EXT": "0000000123"}], "duplicate BP_EXT"),
        "empty": ([{**FULL, "BUT000_BP_EXT": None}], "partner key"),
        "toolong": ([{**FULL, "BUT000_BP_EXT": "12345678901"}], "longer than 10"),
    }
    for name, (rows, message) in cases.items():
        out = tmp_path / f"{name}.xlsx"
        with pytest.raises(CockpitConsistencyError, match=message):
            CockpitGenerator().generate(write_full_staging(tmp_path / f"{name}.parquet", rows), out)
        assert not out.exists() and not out.with_suffix(".xlsx.tmp").exists()


def test_cockpit_drops_incomplete_company_and_sales_rows_with_warnings(tmp_path):
    rows = [{**FULL, "KNB1_BUKRS": None}, {**FULL, "_SRC_PK": "2", "BUT000_BP_EXT": "2", "KNVV_VTWEG": None}]
    result = CockpitGenerator().generate(write_full_staging(tmp_path / "v.parquet", rows), tmp_path / "w.xlsx")
    assert result.sheet_rows[COMPANY] == 1 and result.sheet_rows[SALES] == 1
    assert any("without BUKRS" in w for w in result.warnings) and any("VKORG/VTWEG/SPART" in w for w in result.warnings)


def test_workbook_validation_detects_orphans_and_missing_roles(tmp_path):
    staging = write_full_staging(tmp_path / "v.parquet", [FULL, {**FULL, "_SRC_PK": "2", "BUT000_BP_EXT": "2"}])
    good = CockpitGenerator().generate(staging, tmp_path / "good.xlsx").path

    def tampered(edit):
        out = tmp_path / "tampered.xlsx"
        with zipfile.ZipFile(good) as src, zipfile.ZipFile(out, "w") as dst:
            for item in src.namelist():
                data = src.read(item)
                dst.writestr(item, edit(item, data))
        return validate_workbook(out)

    orphan = tampered(lambda n, d: d.replace(b"</sheetData>", b'<row r="99"><c r="A99" t="inlineStr"><is><t>9999999999</t></is></c>'
                                             b"</row></sheetData>") if n == "xl/worksheets/sheet3.xml" else d)
    assert any("9999999999" in i and "no General Data row" in i for i in orphan)

    no_role = tampered(lambda n, d: re.sub(rb'<row r="[23]">.*?</row>', b"", d) if n == "xl/worksheets/sheet2.xml" else d)
    assert any("has no role" in i for i in no_role)

    dupe_key = tampered(lambda n, d: d.replace(b"0000000002", b"0000000123") if n == "xl/worksheets/sheet1.xml" else d)
    assert any("duplicate BP_EXT" in i for i in dupe_key)


def test_cockpit_from_real_pipeline_output(ready, tmp_path):
    result = CockpitGenerator().generate(ready.valid_path, tmp_path / "cockpit.xlsx")
    valid = ready.report.valid_records
    assert result.business_partners == valid == result.sheet_rows[GENERAL]
    assert result.sheet_rows[ROLES] == 2 * valid and result.sheet_rows[ADDRESS] == valid
    assert result.sheet_rows[COMPANY] == 0 and any("without BUKRS" in w for w in result.warnings)
    keys = [r[0] for r in list(iter_sheet_rows(result.path, GENERAL))[1:]]
    assert len(set(keys)) == len(keys) and all(re.fullmatch(r"\d{10}", k) for k in keys)
    assert validate_workbook(result.path) == []


# ------------------------------------------------------------------ 2. sandbox simulator + OData adapter
def test_sandbox_simulator_behaves_like_a_bapi():
    sandbox = SandboxSapClient(first_partner=5000)
    base = next(iter_records_from_dict(FULL))
    ok = sandbox.create_business_partners([base])[0]
    assert ok.success and ok.partner == "0000005000" and any(m.TYPE == "S" for m in ok.messages)

    dup = sandbox.create_business_partners([base])[0]
    assert not dup.success and dup.messages[0].TYPE == "E" and "already exists" in dup.error_text()

    bad = base.model_copy(update={"bp_ext": "0000000999", "general": {**base.general, "TYPE": "9"}})
    assert "category 9 is invalid" in sandbox.create_business_partners([bad])[0].error_text()
    no_name = base.model_copy(update={"bp_ext": "0000000998", "general": {"TYPE": "2", "BU_GROUP": "BP01"}})
    assert "Name 1 is required" in sandbox.create_business_partners([no_name])[0].error_text()

    warn = base.model_copy(update={"bp_ext": "0000000997", "address": {"COUNTRY": "US"}})
    result = sandbox.create_business_partners([warn])[0]
    assert result.success and result.partner == "0000005001" and any(m.TYPE == "W" for m in result.messages)
    assert sandbox.batch_sizes == [1, 1, 1, 1, 1]


def iter_records_from_dict(row):
    from src.services.bp_model import assemble_record
    yield assemble_record({k: (None if v is None else str(v)) for k, v in row.items()}, BpConfig(), False)


class FakeSap:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.csrf_calls = 0
        self.expire_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            self.csrf_calls += 1
            return httpx.Response(200, headers={"x-csrf-token": f"tok{self.csrf_calls}"}, json={})
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF token validation failed")
        body = json.loads(request.content)
        name = body.get("OrganizationBPName1", "")
        if "BAD" in name:
            return httpx.Response(400, json={"error": {"code": "R1/006", "message": {"lang": "en", "value": "Category missing"},
                                                       "innererror": {"errordetails": [
                                                           {"code": "R1/011", "message": "Country required", "severity": "error"},
                                                           {"code": "R1/050", "message": "Region empty", "severity": "warning"}]}}})
        if "DOWN" in name:
            return httpx.Response(503, text="maintenance")
        return httpx.Response(201, json={"d": {"BusinessPartner": f"00001{len(self.requests):05d}"}})


def http_client(fake, **config):
    cfg = SapConfig(base_url="https://sap.example.test", user="MIGUSER", password="pw", client="100", **config)
    return HttpSapClient(cfg, transport=httpx.MockTransport(fake), retry_delay=0)


def test_odata_payload_is_a_deep_insert():
    rec = next(iter_records_from_dict(FULL))
    body = build_odata_payload(rec, external_id_field="YY1_LegacyKey")
    assert body["BusinessPartnerCategory"] == "2" and body["OrganizationBPName1"] == "Acme & Sons <GmbH>"
    assert body["YY1_LegacyKey"] == "0000000123"
    assert [r["BusinessPartnerRole"] for r in body["to_BusinessPartnerRole"]["results"]] == ["FLCU00", "FLCU01"]
    assert body["to_BusinessPartnerAddress"]["results"][0] == {
        "Country": "DE", "StreetName": "Hauptstr.", "HouseNumber": "7", "CityName": "Berlin", "PostalCode": "01067",
        "Region": "BE", "Language": "D"}
    customer = body["to_Customer"]
    assert customer["to_CustomerCompany"]["results"][0]["ReconciliationAccount"] == "0000113100"
    assert customer["to_CustomerSalesArea"]["results"][0]["SalesOrganization"] == "1010"
    assert "YY1_LegacyKey" not in build_odata_payload(rec)


def test_http_adapter_success_error_mapping_and_csrf():
    fake = FakeSap()
    client = http_client(fake)
    good = next(iter_records_from_dict(FULL))
    bad = good.model_copy(update={"source_pk": "2", "general": {**good.general, "NAME_ORG1": "BAD name"}})
    r_good, r_bad = client.create_business_partners([good, bad])

    assert r_good.success and r_good.partner and r_good.messages[0].TYPE == "S"
    assert not r_bad.success and r_bad.partner is None
    assert [(m.TYPE, m.ID, m.NUMBER) for m in r_bad.messages] == [("E", "R1", "006"), ("E", "R1", "011"), ("W", "R1", "050")]
    assert "Category missing" in r_bad.error_text() and "Region empty" not in r_bad.error_text()  # warnings are not errors

    posts = [r for r in fake.requests if r.method == "POST"]
    assert fake.csrf_calls == 1 and all(r.headers["x-csrf-token"] == "tok1" for r in posts)  # fetched once, reused
    assert all(r.headers["authorization"].startswith("Basic ") for r in fake.requests)
    assert all(r.url.params.get("sap-client") == "100" for r in fake.requests)

    fake.expire_once = True  # expired token -> refreshed and the record retried once
    again = client.create_business_partners([good])[0]
    assert again.success and fake.csrf_calls == 2


def test_http_adapter_auth_and_outage_abort():
    good = next(iter_records_from_dict(FULL))
    denied = HttpSapClient(SapConfig("https://sap.example.test", "u", "p"), retry_delay=0,
                           transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(SapAuthError):
        denied.create_business_partners([good])
    down = good.model_copy(update={"general": {**good.general, "NAME_ORG1": "DOWN"}})
    with pytest.raises(SapUnavailableError):
        http_client(FakeSap()).create_business_partners([down])


def test_sap_config_from_environment():
    assert SapConfig.from_env({}) is None
    cfg = SapConfig.from_env({"SAP_BASE_URL": "https://s/", "SAP_USER": "u", "SAP_PASSWORD": "s3cr3t-pw", "SAP_CLIENT": "200"})
    assert (cfg.base_url, cfg.client) == ("https://s", "200") and "s3cr3t-pw" not in repr(cfg)


# ------------------------------------------------------------------ 3. loader: gate
def test_loader_blocks_unapproved_run_before_reading_any_row(pipe, session_factory):
    sandbox = SandboxSapClient()
    svc = loader(session_factory, sandbox)
    assert svc.is_ready_for_load(pipe.run.run_id) is False
    with pytest.raises(GateBlockedError) as exc:
        svc.load(pipe.run.run_id, pipe.valid_path)
    assert any("APPROVED" in r for r in exc.value.result.reasons) and any("CRITICAL" in r for r in exc.value.result.reasons)
    assert sandbox.batch_sizes == [] and not lineage(session_factory, pipe.run.run_id, target_column_name=PARTNER_COLUMN)
    row = run_row(session_factory, pipe.run.run_id)
    assert row.current_phase == MigrationPhase.VALIDATE and row.records_loaded == 0


def test_loader_blocks_approved_mapping_with_open_critical_defects(pipe, session_factory):
    submit_and_approve(pipe.gov, pipe.env["mapping"].id)  # approved, but the dirty data's CRITICAL defects remain
    sandbox = SandboxSapClient()
    result = pipe.gov.evaluate_readiness(pipe.run.run_id)
    assert result.mapping_status == "APPROVED" and result.open_critical_defects > 0
    with pytest.raises(GateBlockedError, match="CRITICAL"):
        loader(session_factory, sandbox).load(pipe.run.run_id, pipe.valid_path)
    assert sandbox.batch_sizes == []


def test_loader_rejects_unknown_runs_and_dirty_input_files(ready, session_factory):
    sandbox = SandboxSapClient()
    svc = loader(session_factory, sandbox)
    with pytest.raises(NotFoundError):
        svc.load(999_999, ready.valid_path)
    for bad in (ready.report.rejected_path, ready.report.duplicates_path):
        with pytest.raises(LoadInputError):
            svc.load(ready.run.run_id, bad)
    with pytest.raises(FileNotFoundError):
        svc.load(ready.run.run_id, "missing.parquet")
    assert sandbox.batch_sizes == []
    assert run_row(session_factory, ready.run.run_id).current_phase == MigrationPhase.VALIDATE  # not claimed


# ------------------------------------------------------------------ 3b. loader: ingestion
def test_successful_load_assigns_sap_numbers_and_updates_lineage(ready, session_factory):
    sandbox = SandboxSapClient(first_partner=1000000001)
    before = run_row(session_factory, ready.run.run_id)
    transform_failed = before.records_failed
    result = loader(session_factory, sandbox).load(ready.run.run_id, ready.valid_path)

    valid = ready.report.valid_records
    assert (result.attempted, result.loaded, result.failed, result.status) == (valid, valid, 0, "SUCCEEDED")
    assert sandbox.batch_sizes == [100] * (valid // 100) + ([valid % 100] if valid % 100 else [])
    assert result.batches == len(sandbox.batch_sizes) and result.batch_size == 100

    row = run_row(session_factory, ready.run.run_id)
    assert row.current_phase == MigrationPhase.LOAD and row.status == RunStatus.SUCCEEDED
    assert row.records_loaded == valid and row.records_failed == transform_failed  # transform failures preserved
    assert row.completed_at and row.completed_at >= row.started_at

    partner_rows = lineage(session_factory, ready.run.run_id, target_column_name=PARTNER_COLUMN)
    assert len(partner_rows) == valid and all(r.loaded_successfully and r.error_message is None for r in partner_rows)
    numbers = [r.target_pk_value for r in partner_rows]
    assert len(set(numbers)) == valid and all(re.fullmatch(r"\d{10}", n) for n in numbers)
    assert sorted(numbers)[0] == "1000000001" and sorted(numbers)[-1] == str(1000000000 + valid)

    # the Module 4 cell lineage of a loaded record now carries the SAP number
    pk = partner_rows[0].source_pk_value
    cells = lineage(session_factory, ready.run.run_id, source_pk_value=pk)
    assert len(cells) > 1 and {c.target_pk_value for c in cells} == {partner_rows[0].target_pk_value}
    assert all(c.loaded_successfully for c in cells)

    # duplicates and rejected records were never loaded
    loaded_pks = {r.source_pk_value for r in partner_rows}
    dup_pks = {r["_SRC_PK"] for r in read_parquet(ready.report.duplicates_path)}
    assert not (loaded_pks & dup_pks)
    assert not any(c.loaded_successfully for c in lineage(session_factory, ready.run.run_id) if c.source_pk_value in dup_pks)


def test_load_failures_capture_the_exact_sap_message(ready, session_factory):
    sandbox = SandboxSapClient(reject=lambda rec: BapiRet2(
        TYPE="E", ID="R1", NUMBER="099", MESSAGE=f"Tax number invalid for {rec.bp_ext}") if rec.bp_ext.endswith("7") else None)
    before = run_row(session_factory, ready.run.run_id).records_failed
    result = loader(session_factory, sandbox).load(ready.run.run_id, ready.valid_path)

    assert result.failed > 0 and result.loaded + result.failed == ready.report.valid_records
    assert result.status == "FAILED" and len(result.error_samples) <= 20
    row = run_row(session_factory, ready.run.run_id)
    assert row.status == RunStatus.FAILED and row.current_phase == MigrationPhase.LOAD
    assert row.records_loaded == result.loaded and row.records_failed == before + result.failed

    failed_rows = [r for r in lineage(session_factory, ready.run.run_id, target_column_name=PARTNER_COLUMN)
                   if not r.loaded_successfully]
    assert len(failed_rows) == result.failed
    for r in failed_rows:
        assert r.target_pk_value is None and r.transformed_value is None
        assert re.search(r"\[E\] R1/099: Tax number invalid for \d{10}", r.error_message)
    # every cell row of a failed record carries the SAP error and stays unloaded
    pk = failed_rows[0].source_pk_value
    cells = lineage(session_factory, ready.run.run_id, source_pk_value=pk)
    assert all(not c.loaded_successfully and c.target_pk_value is None for c in cells)
    assert all("Tax number invalid" in (c.error_message or "") for c in cells)


def test_loader_cannot_run_twice(ready, session_factory):
    sandbox = SandboxSapClient()
    svc = loader(session_factory, sandbox)
    svc.load(ready.run.run_id, ready.valid_path)
    calls = list(sandbox.batch_sizes)
    with pytest.raises(LoadStateError, match="only once"):
        svc.load(ready.run.run_id, ready.valid_path)
    assert sandbox.batch_sizes == calls  # nothing re-sent


def test_sap_outage_mid_load_fails_the_run_but_keeps_committed_batches(ready, session_factory):
    class Flaky(SandboxSapClient):
        def create_business_partners(self, records):
            if len(self.batch_sizes) == 2:
                raise SapUnavailableError("SAP went away")
            return super().create_business_partners(records)

    with pytest.raises(SapUnavailableError):
        loader(session_factory, Flaky()).load(ready.run.run_id, ready.valid_path)
    row = run_row(session_factory, ready.run.run_id)
    assert row.status == RunStatus.FAILED and row.current_phase == MigrationPhase.LOAD and row.completed_at
    assert row.records_loaded == 200  # the two committed batches are on record
    assert len(lineage(session_factory, ready.run.run_id, target_column_name=PARTNER_COLUMN, loaded_successfully=True)) == 200


def test_record_without_partner_key_fails_locally_and_batch_size_is_validated(env, session_factory, tmp_path):
    run_id = make_run(session_factory, env["mapping"].id)
    staging = write_staging(tmp_path / "s.parquet", [{"pk": "A1"}, {"pk": "A2", "set": {"BUT000_BP_EXT": None}}])
    report = ValidationEngine(session_factory).validate(env["mapping"].id, staging, tmp_path / "v")
    DefectService(session_factory).record_defects(run_id, report)
    gov = GovernanceService(session_factory)
    submit_and_approve(gov, env["mapping"].id)

    sandbox = SandboxSapClient()
    result = loader(session_factory, sandbox, gov, batch_size=1).load(run_id, report.valid_path)
    assert (result.loaded, result.failed, result.batches) == (1, 1, 2)
    assert sandbox.batch_sizes == [1]  # the keyless record never reached SAP
    failed = lineage(session_factory, run_id, source_pk_value="A2")[0]
    assert "not sent to SAP" in failed.error_message and failed.target_pk_value is None
    with pytest.raises(ValueError):
        loader(session_factory, sandbox, gov, batch_size=0)


# ------------------------------------------------------------------ 4. reconciliation
def test_reconciliation_accounts_for_every_source_row(ready, session_factory):
    loader(session_factory, SandboxSapClient()).load(ready.run.run_id, ready.valid_path)
    report = ReconciliationService(session_factory).generate_reconciliation_report(ready.run.run_id, ready.report)
    c = report.counts

    assert c.extracted == ready.run.source_records and c.transformed_ok == ready.run.transformed_records
    assert c.transform_failed == ready.run.failed_records
    assert c.duplicate_children == ready.run.deduplication.duplicate_children > 0
    assert c.valid == ready.report.valid_records and c.validation_rejected == ready.report.defective_records
    assert (c.loaded, c.load_failed, c.not_attempted, c.load_attempted) == (c.valid, 0, 0, c.valid)

    # the identity: extracted = duplicates + rejected + loaded + failed + not attempted
    assert c.extracted == c.duplicate_children + c.validation_rejected + c.loaded + c.load_failed + c.not_attempted
    assert report.balanced and report.variance == 0 and report.accounted_rows == c.extracted
    assert {d.reason: d.count for d in report.dropoff} == {
        "LOADED": c.loaded, "SAP_LOAD_FAILED": 0, "VALIDATION_REJECTED": c.validation_rejected,
        "DUPLICATE_CHILD": c.duplicate_children, "VALID_NOT_ATTEMPTED": 0}
    rejected = next(d for d in report.dropoff if d.reason == "VALIDATION_REJECTED")
    assert rejected.sample_pks and "ERR_" in rejected.explanation and f"{c.transform_failed} of these" in rejected.explanation

    assert report.complete and report.passed and all(k.passed for k in report.checks), [k for k in report.checks if not k.passed]
    assert report.lineage_sample.sampled == 20 and report.lineage_sample.passed == 20 and not report.lineage_sample.failures
    assert report.lineage_sample.examples and "-> SAP" in report.lineage_sample.examples[0]
    assert report.run_status == "SUCCEEDED" and report.run_phase == "LOAD" and report.open_critical_defects == 0
    assert report.mapping_approved_by == "data.steward" and report.mapping_created_by == "ai-mapper"
    assert report.validation_failures == {f.signature: f.violation_count for f in ready.report.failures}


def test_reconciliation_with_partial_load_failures_and_aborted_load(ready, session_factory):
    class Stops(SandboxSapClient):
        def create_business_partners(self, records):
            if len(self.batch_sizes) == 3:
                raise SapUnavailableError("down")
            return super().create_business_partners(records)

    rejecting = Stops(reject=lambda rec: BapiRet2(TYPE="E", ID="R1", NUMBER="099", MESSAGE="<b>Bad</b> address")
                      if rec.bp_ext.endswith("3") else None)
    with pytest.raises(SapUnavailableError):
        loader(session_factory, rejecting).load(ready.run.run_id, ready.valid_path)

    report = ReconciliationService(session_factory).generate_reconciliation_report(ready.run.run_id, ready.report)
    c = report.counts
    assert c.load_failed > 0 and c.loaded > 0 and c.not_attempted == c.valid - c.load_attempted > 0
    assert c.load_attempted == 300
    assert report.balanced and report.variance == 0
    assert {d.reason: d.count for d in report.dropoff}["VALID_NOT_ATTEMPTED"] == c.not_attempted
    assert report.run_status == "FAILED" and report.run_phase == "LOAD"
    assert report.load_errors == {"[E] R1/099: <b>Bad</b> address": c.load_failed}
    assert report.passed  # failures are accounted for; reconciliation passes when the books balance

    md, html_text = render_markdown(report), render_html(report)
    assert f"Run {ready.run.run_id}" in md and "VALID_NOT_ATTEMPTED" in md and "Sign-off" in md
    assert "&lt;b&gt;Bad&lt;/b&gt;" in html_text and "<b>Bad</b>" not in html_text  # HTML-escaped


def test_reconciliation_without_validation_report_is_flagged_incomplete(ready, session_factory):
    loader(session_factory, SandboxSapClient()).load(ready.run.run_id, ready.valid_path)
    report = ReconciliationService(session_factory).generate_reconciliation_report(ready.run.run_id)
    assert not report.complete and not report.passed and report.balanced
    assert report.counts.valid is None and report.counts.duplicate_children is None
    assert [d.reason for d in report.dropoff] == ["LOADED", "SAP_LOAD_FAILED", "NOT_LOADED_BEFORE_SAP"]
    assert not next(k for k in report.checks if k.name == "validation_report_supplied").passed


def test_reconciliation_detects_tampering(ready, session_factory):
    loader(session_factory, SandboxSapClient()).load(ready.run.run_id, ready.valid_path)
    svc = ReconciliationService(session_factory)
    assert svc.generate_reconciliation_report(ready.run.run_id, ready.report).passed

    with session_factory.begin() as s:  # counter no longer matches lineage
        s.execute(sa.update(MigMigrationRun).where(MigMigrationRun.id == ready.run.run_id)
                  .values(records_loaded=MigMigrationRun.records_loaded + 5))
    tampered = svc.generate_reconciliation_report(ready.run.run_id, ready.report)
    assert not tampered.passed and not next(k for k in tampered.checks if k.name == "run_counter_loaded_matches_lineage").passed

    with session_factory.begin() as s:  # a loaded record loses its SAP number
        s.execute(sa.update(MigMigrationRun).where(MigMigrationRun.id == ready.run.run_id)
                  .values(records_loaded=MigMigrationRun.records_loaded - 5))
        s.execute(sa.text("UPDATE mig_cell_lineage SET target_pk_value = NULL WHERE id = ("
                          "SELECT min(id) FROM mig_cell_lineage WHERE run_id = :r AND target_column_name <> :p "
                          "AND loaded_successfully)"), {"r": ready.run.run_id, "p": PARTNER_COLUMN})
    broken = svc.generate_reconciliation_report(ready.run.run_id, ready.report)
    assert not broken.passed and (broken.lineage_sample.failures or not next(
        k for k in broken.checks if k.name == "every_loaded_record_has_sap_number").passed)


def test_reconciliation_export_and_unknown_run(ready, session_factory, tmp_path):
    loader(session_factory, SandboxSapClient()).load(ready.run.run_id, ready.valid_path)
    svc = ReconciliationService(session_factory)
    report = svc.generate_reconciliation_report(ready.run.run_id, ready.report)
    paths = svc.export(report, tmp_path / "audit")
    md = open(paths["markdown"], encoding="utf-8").read()
    html_text = open(paths["html"], encoding="utf-8").read()
    assert "Result: PASSED" in md and f"{report.counts.extracted:,}" in md and "| Auditor |" in md
    assert "<table>" in html_text and "Result: PASSED" in html_text and html_text.startswith("<!doctype html>")
    with pytest.raises(NotFoundError):
        svc.generate_reconciliation_report(999_999)


# ------------------------------------------------------------------ headline: full 5,000+ row dataset
def test_full_dataset_end_to_end_cockpit_load_and_reconciliation(env, session_factory, csv_path, dataset, tmp_path):
    pipeline = make_loadable(run_pipeline(env, session_factory, tmp_path, csv_path), session_factory, tmp_path)
    assert len(dataset) >= 5000 and pipeline.report.valid_records > 4000

    cockpit = CockpitGenerator().generate(pipeline.valid_path, tmp_path / "cockpit.xlsx")
    assert cockpit.business_partners == pipeline.report.valid_records and validate_workbook(cockpit.path) == []

    result = loader(session_factory, SandboxSapClient()).load(pipeline.run.run_id, pipeline.valid_path)
    assert result.loaded == pipeline.report.valid_records and result.failed == 0 and result.status == "SUCCEEDED"
    assert result.batches == -(-result.attempted // 100)

    recon = ReconciliationService(session_factory).generate_reconciliation_report(pipeline.run.run_id, pipeline.report)
    assert recon.passed and recon.variance == 0 and recon.counts.extracted == len(dataset)
    assert recon.counts.extracted == (recon.counts.duplicate_children + recon.counts.validation_rejected + recon.counts.loaded)
    # the cockpit and the loader describe exactly the same set of partners
    cockpit_keys = {r[0] for r in list(iter_sheet_rows(cockpit.path, GENERAL))[1:]}
    assert cockpit_keys == {r.bp_ext for r in iter_bp_records(pipeline.valid_path)}
