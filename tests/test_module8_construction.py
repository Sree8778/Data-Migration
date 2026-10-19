"""Module 8 tests: Data Construction (missing mandatory data filled through Excel).

Scenario: a legacy customer extract where 50 of 300 records lack mandatory SAP values (BU_GROUP, AKONT,
sometimes COUNTRY). The gate is BLOCKED by CRITICAL defects; a business user fills an Excel template;
the values are merged with lineage; re-validation opens the gate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from src.api.context import AppContext
from src.api.main import create_app
from src.api.settings import Settings
from src.db.enums import MigrationPhase
from src.db.models import MapFieldRule, MigCellLineage, MigMigrationRun
from src.schemas.execution import DedupConfig
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    DataSourceCreate,
    MappingSetCreate,
    TableCreate,
)
from src.schemas.validation import GateState, Severity
from src.db.enums import RuleType, SystemType, TargetType
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.construction_service import (
    DATA_SHEET,
    LISTS_SHEET,
    META_SHEET,
    ConstructionInputError,
    ConstructionService,
    ConstructionStateError,
)
from src.services.defect_service import DefectService
from src.services.governance_service import GovernanceService
from src.services.metadata_service import NotFoundError
from src.services.validation_engine import ValidationEngine

STEWARD, AUTHOR = "data.steward", "ai-mapper"
HEADER = ["CUST_ID", "CUST_NAME", "CITY", "COUNTRY", "GROUP_CD", "RECON_ACCT"]
DEDUP = DedupConfig(name_column="BUT000_NAME_ORG1", address_columns=["ADRC_CITY1"])
SYL = ["ka", "lo", "mi", "ra", "ve", "tu", "zo", "pe"]


def needs_for(i: int) -> set[str]:
    """Which mandatory fields record i is missing (every 6th record is incomplete -> 50 records)."""
    if i % 6:
        return set()
    k = i // 6
    out = {"BU_GROUP"} if k % 3 == 0 else {"AKONT"} if k % 3 == 1 else {"BU_GROUP", "AKONT"}
    if k % 5 == 0:
        out.add("COUNTRY")
    return out


def build_rows() -> list[dict]:
    rows = []
    for i in range(300):
        name = "".join(SYL[(i // 8 ** j) % 8] for j in range(4)).capitalize() + f" Trading {i:03d}"
        miss = needs_for(i)
        rows.append({"CUST_ID": str(5000 + i), "CUST_NAME": name, "CITY": "Boston",
                     "COUNTRY": "" if "COUNTRY" in miss else ["US", "DE", "FR"][i % 3],
                     "GROUP_CD": "" if "BU_GROUP" in miss else "BP01",
                     "RECON_ACCT": "" if "AKONT" in miss else "113100"})
    return rows


EXPECTED_FIELD_COUNTS = {"BU_GROUP": 0, "AKONT": 0, "COUNTRY": 0}
for _i in range(0, 300, 6):
    for _f in needs_for(_i):
        EXPECTED_FIELD_COUNTS[_f] += 1
EXPECTED_CELLS = sum(EXPECTED_FIELD_COUNTS.values())


@dataclass
class Scenario:
    ctx: AppContext
    client: TestClient
    construction: ConstructionService
    governance: GovernanceService
    mapping_id: int
    run_id: int
    run_dir: Path
    rule_ids: dict
    rows: list[dict]


def write_csv(path: Path, rows) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture()
def scenario(service, session_factory, tmp_path) -> Scenario:
    ctx = AppContext(Settings(workspace_dir=tmp_path / "ws", auto_seed=False), session_factory)
    seed_sap_catalog(service)
    source = service.register_data_source(DataSourceCreate(
        system_name="LEGACY_AR", system_type=SystemType.ORACLE, target_type=TargetType.SOURCE))
    table = service.register_table(TableCreate(source_id=source.id, table_name="AR_CUSTOMERS"))
    cols = {c.column_name: c.id for c in service.bulk_register_columns(BulkColumnRegistration(table_id=table.id, columns=[
        ColumnCreate(column_name=h, data_type="VARCHAR2", char_max_length=100, is_primary_key=(h == "CUST_ID")) for h in HEADER]))}
    target = {(t.table.table_name, c.column_name): c.id for t in service.get_target_dictionary().tables for c in t.columns}
    but000 = next(t.table for t in service.get_target_dictionary().tables if t.table.table_name == "BUT000")
    mapping = service.create_mapping_set(MappingSetCreate(
        source_table_id=table.id, target_table_id=but000.id, created_by=AUTHOR))
    rules = [("CUST_ID", "BUT000", "BP_EXT", RuleType.SQL_EXPRESSION, "LPAD(TRIM(CUST_ID), 10, '0')"),
             ("CUST_NAME", "BUT000", "NAME_ORG1", RuleType.DIRECT_COPY, None),
             ("CITY", "ADRC", "CITY1", RuleType.DIRECT_COPY, None),
             ("COUNTRY", "ADRC", "COUNTRY", RuleType.DIRECT_COPY, None),
             ("GROUP_CD", "BUT000", "BU_GROUP", RuleType.DIRECT_COPY, None),
             ("RECON_ACCT", "KNB1", "AKONT", RuleType.DIRECT_COPY, None),
             (None, "BUT000", "TYPE", RuleType.STATIC_VALUE, "'2'")]
    rule_ids = {}
    with session_factory.begin() as s:
        for src, tt, tc, rtype, logic in rules:
            r = MapFieldRule(mapping_set_id=mapping.id, source_column_id=cols[src] if src else None,
                             target_column_id=target[(tt, tc)], rule_type=rtype, transformation_logic=logic)
            s.add(r)
            s.flush()
            rule_ids[f"{tt}.{tc}"] = r.id

    rows = build_rows()
    csv_file = write_csv(tmp_path / "legacy.csv", rows)
    run = ctx.executor.execute_run(mapping.id, csv_file, "WAVE_1", DEDUP, passthrough_columns=["COUNTRY", "GROUP_CD"])
    run_dir = ctx.run_dir(run.run_id)
    report = ctx.validation.validate(mapping.id, run.deduplicated_path, run_dir / "validation", run_id=run.run_id)
    (run_dir / "validation.json").write_text(report.model_dump_json(), encoding="utf-8")
    ctx.defects.record_defects(run.run_id, report)
    governance = ctx.governance
    governance.submit_mapping_set(mapping.id, AUTHOR)
    governance.approve_mapping_set(mapping.id, STEWARD)
    client = TestClient(create_app(ctx), headers={"X-User": STEWARD})
    return Scenario(ctx, client, ctx.construction, governance, mapping.id, run.run_id, run_dir, rule_ids, rows)


# ------------------------------------------------------------------ helpers
def fill(template: str | Path, out: Path, value_for, only_rows=None) -> Path:
    """Act as the business user: fill the unlocked (highlighted) cells of every data row."""
    wb = load_workbook(template)
    ws = wb[DATA_SHEET]
    headers = {c.value: c.column for c in ws[1]}
    for r in range(2, ws.max_row + 1):
        row = {h: ws.cell(row=r, column=col).value for h, col in headers.items()}
        for h, col in headers.items():
            cell = ws.cell(row=r, column=col)
            if cell.protection.locked is False:
                value = value_for(h, row)
                if value is not None:
                    cell.value = value
    wb.save(out)
    return out


def good_values(header, row):
    return {"BU_GROUP": "BP01", "AKONT": "113100", "COUNTRY": "DE"}[header]


def read_parquet(path):
    con = duckdb.connect()
    cur = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def constructed_lineage(sf, run_id):
    with sf() as s:
        return s.scalars(sa.select(MigCellLineage).where(
            MigCellLineage.run_id == run_id, MigCellLineage.error_message.like("[CONSTRUCTED]%")).order_by(MigCellLineage.id)).all()


def pending(sc: Scenario):
    return sc.construction.get_summary(sc.run_id)


# ------------------------------------------------------------------ baseline: the gate is blocked
def test_scenario_starts_blocked_by_missing_mandatory_data(scenario):
    sc = scenario
    readiness = sc.governance.evaluate_readiness(sc.run_id)
    assert readiness.state == GateState.BLOCKED and readiness.open_critical_defects == 3
    summary = pending(sc)
    assert summary.status == "PENDING" and summary.can_construct
    assert summary.incomplete_records == 50 and summary.pending_cells == EXPECTED_CELLS
    assert {f.target: f.records for f in summary.required_fields} == {
        "BUT000.BU_GROUP": EXPECTED_FIELD_COUNTS["BU_GROUP"], "KNB1.AKONT": EXPECTED_FIELD_COUNTS["AKONT"],
        "ADRC.COUNTRY": EXPECTED_FIELD_COUNTS["COUNTRY"]}


# ------------------------------------------------------------------ 1. template
def test_template_structure_protection_and_dropdowns(scenario, tmp_path):
    sc = scenario
    result = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx", user=STEWARD)
    assert result.records == 50 and not result.truncated and result.total_records_needing == 50
    assert result.pending_cells == EXPECTED_CELLS
    assert {f.header: f.records_needing for f in result.fields} == EXPECTED_FIELD_COUNTS
    assert zipfile.is_zipfile(result.path)

    wb = load_workbook(result.path)
    assert wb.sheetnames == ["Instructions", DATA_SHEET, LISTS_SHEET, META_SHEET]
    assert wb[LISTS_SHEET].sheet_state == "hidden" and wb[META_SHEET].sheet_state == "hidden"
    ws = wb[DATA_SHEET]
    headers = [c.value for c in ws[1]]
    assert headers[0] == "SOURCE_PK" and "NAME_ORG1" in headers and headers[-1] == "ROW_REF"
    assert {"BU_GROUP", "AKONT", "COUNTRY"} <= set(headers) and "OTHER_ISSUES" in headers
    assert "LEGACY_COUNTRY" in headers and "LEGACY_GROUP_CD" in headers  # what the legacy system said, read-only
    assert ws.max_row == 51 and ws.protection.sheet is True and ws.freeze_panes == "B2"

    col = {h: i + 1 for i, h in enumerate(headers)}
    unlocked = 0
    for r in range(2, ws.max_row + 1):
        pk, ref = ws.cell(row=r, column=col["SOURCE_PK"]), ws.cell(row=r, column=col["ROW_REF"])
        assert pk.protection.locked and ref.protection.locked and ws.cell(row=r, column=col["NAME_ORG1"]).protection.locked
        assert isinstance(ref.value, int) and ws.cell(row=r, column=col["NAME_ORG1"]).value.endswith("Trading " + str(int(pk.value) - 5000).zfill(3))
        for h in ("BU_GROUP", "AKONT", "COUNTRY"):
            cell = ws.cell(row=r, column=col[h])
            assert cell.value is None
            if not cell.protection.locked:
                unlocked += 1
                assert cell.number_format == "@" and cell.fill.fgColor.rgb.endswith("FEF3C7")  # highlighted
    assert unlocked == EXPECTED_CELLS  # exactly the cells that need a value are editable

    validations = {str(dv.sqref).split(":")[0][0]: dv for dv in ws.data_validations.dataValidation}
    country, group, akont = (validations[get(col, h)] for h in ("COUNTRY", "BU_GROUP", "AKONT"))
    assert country.type == "list" and LISTS_SHEET in country.formula1  # ISO country drop-down from the domain provider
    lists = wb[LISTS_SHEET]
    iso = {c.value for c in lists["A"] if c.value}
    assert {"US", "DE", "JP"} <= iso and "XX" not in iso
    assert (group.type, group.formula1) == ("textLength", "4") and (akont.type, akont.formula1) == ("textLength", "10")
    meta = {r[0].value: r[1].value for r in wb[META_SHEET].iter_rows(min_row=1, max_row=5)}
    assert meta["run_id"] == sc.run_id and meta["template_id"] == result.template_id


def get(col: dict, header: str) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(col[header])


def test_template_context_is_never_executable_and_truncation_is_reported(scenario, tmp_path):
    sc = scenario
    # legacy text that Excel would treat as a formula must stay inert text
    cdir_staging = sc.run_dir / "staging.parquet"
    con = duckdb.connect()
    for target in (cdir_staging, sc.run_dir / "deduplicated.parquet"):
        con.execute(f"COPY (SELECT * REPLACE (CASE WHEN \"_SRC_PK\" = '5000' THEN '=HYPERLINK(\"http://evil\",\"x\")' ELSE \"BUT000_NAME_ORG1\" END "
                    f"AS \"BUT000_NAME_ORG1\") FROM read_parquet('{target.as_posix()}')) TO '{target.as_posix()}.new' (FORMAT parquet)")
        Path(f"{target.as_posix()}.new").replace(target)
    result = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx", max_rows=10)
    assert result.records == 10 and result.truncated and result.total_records_needing == 50
    ws = load_workbook(result.path)[DATA_SHEET]
    headers = [c.value for c in ws[1]]
    name_cell = ws.cell(row=2, column=headers.index("NAME_ORG1") + 1)
    assert name_cell.value.startswith("=HYPERLINK") and name_cell.data_type == "s"


# ------------------------------------------------------------------ 2. ingest + lineage
def test_ingest_merges_values_validates_cells_and_writes_lineage(scenario, session_factory, tmp_path):
    sc = scenario
    original_staging = sha(sc.run_dir / "staging.parquet")
    original_dedup = sha(sc.run_dir / "deduplicated.parquet")
    before = {r["_SRC_PK"]: r for r in read_parquet(sc.run_dir / "staging.parquet")}
    template = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx").path

    bad_country_pk = {"done": False}

    def first_pass(header, row):
        if header == "COUNTRY" and not bad_country_pk["done"]:
            bad_country_pk["done"] = True
            return "XX"  # not an ISO country
        if header == "AKONT" and row["SOURCE_PK"] == "5006":
            return "12345678901"  # 11 characters, AKONT allows 10
        return good_values(header, row)

    filled = fill(template, tmp_path / "filled1.xlsx", first_pass)
    r1 = sc.construction.ingest_constructed_data(sc.run_id, filled, STEWARD, filename="filled1.xlsx")
    assert r1.validation_status == "PARTIAL" and r1.cells_rejected == 2
    assert r1.cells_modified == EXPECTED_CELLS - 2 and r1.remaining_cells_pending == 2 and r1.revalidation_required
    assert {(c.field, c.reason) for c in r1.rejected_samples} == {
        ("ADRC.COUNTRY", "not a valid T005 key"), ("KNB1.AKONT", "longer than 10 characters")}
    assert r1.lineage_rows == r1.cells_modified and r1.cells_ignored == 0
    assert r1.records_updated <= 50 and r1.uploaded_by == STEWARD

    # second upload of the corrected sheet: previously merged cells are ignored (no duplicate lineage)
    fixed = fill(template, tmp_path / "filled2.xlsx", good_values)
    r2 = sc.construction.ingest_constructed_data(sc.run_id, fixed, STEWARD, filename="filled2.xlsx")
    assert r2.validation_status == "OK" and r2.cells_modified == 2 and r2.cells_rejected == 0
    assert r2.cells_ignored == EXPECTED_CELLS - 2 and r2.remaining_cells_pending == 0

    # staging reflects the constructed values; originals are untouched
    assert sha(sc.run_dir / "staging.parquet") == original_staging and sha(sc.run_dir / "deduplicated.parquet") == original_dedup
    for path in (sc.run_dir / "construction" / "staging.parquet", sc.run_dir / "construction" / "deduplicated.parquet"):
        after = {r["_SRC_PK"]: r for r in read_parquet(path)}
        assert len(after) == len(before) == 300
        for i in range(300):
            pk = str(5000 + i)
            old, new = before[pk], after[pk]
            miss = needs_for(i)
            if not miss:
                assert new["_TRANSFORM_STATUS"] == "OK" and all(new[k] == old[k] for k in old if not k.startswith("is_dup"))
                continue
            assert new["BUT000_BU_GROUP"] == "BP01"
            assert new["KNB1_AKONT"] == "113100"
            assert (new["ADRC_COUNTRY"] == "DE") if "COUNTRY" in miss else (new["ADRC_COUNTRY"] == old["ADRC_COUNTRY"])
            assert new["_TRANSFORM_STATUS"] == "OK" and new["_TRANSFORM_ERRORS"] is None  # every error of the row is resolved
            for untouched in ("BUT000_BP_EXT", "BUT000_NAME_ORG1", "ADRC_CITY1", "BUT000_TYPE"):
                assert new[untouched] == old[untouched]
    assert {r["is_duplicate_child"] for r in read_parquet(sc.run_dir / "construction" / "deduplicated.parquet")} == {False}

    # one lineage row per merged cell, pointing at the field's rule, with who / when / previous state
    rows = constructed_lineage(session_factory, sc.run_id)
    assert len(rows) == EXPECTED_CELLS == r1.lineage_rows + r2.lineage_rows
    by_target = {}
    for row in rows:
        by_target.setdefault(row.target_column_name, []).append(row)
        assert row.error_message.startswith(f"[CONSTRUCTED] by {STEWARD} at 20") and " via filled" in row.error_message
        assert "previous: " in row.error_message and "is mandatory" in row.error_message
        assert row.source_raw_value is None and row.transformed_value and row.loaded_successfully is False
        assert row.created_at is not None and row.target_pk_value is None
    assert {t: len(v) for t, v in by_target.items()} == {
        "BUT000.BU_GROUP": EXPECTED_FIELD_COUNTS["BU_GROUP"], "KNB1.AKONT": EXPECTED_FIELD_COUNTS["AKONT"],
        "ADRC.COUNTRY": EXPECTED_FIELD_COUNTS["COUNTRY"]}
    for target, group in by_target.items():
        assert {r.rule_applied_id for r in group} == {sc.rule_ids[target]}
    assert {r.transformed_value for r in by_target["BUT000.BU_GROUP"]} == {"BP01"}
    assert len({(r.source_pk_value, r.target_column_name) for r in rows}) == len(rows)  # no duplicates from the re-upload

    summary = pending(sc)
    assert summary.status == "REVALIDATION_REQUIRED"
    assert [(a.event, a.user) for a in summary.activity] == [("template", "system"), ("upload", STEWARD), ("upload", STEWARD)]
    assert [a.detail["cells_modified"] for a in summary.activity if a.event == "upload"] == [EXPECTED_CELLS - 2, 2]


# ------------------------------------------------------------------ 3. re-validation reopens the gate
def test_revalidation_moves_the_gate_from_blocked_to_ready(scenario, session_factory, tmp_path):
    sc = scenario
    assert sc.governance.evaluate_readiness(sc.run_id).state == GateState.BLOCKED
    template = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx").path
    sc.construction.ingest_constructed_data(sc.run_id, fill(template, tmp_path / "f.xlsx", good_values), STEWARD)
    # merged but not yet re-validated: the gate still reflects the old validation
    assert sc.governance.evaluate_readiness(sc.run_id).state == GateState.BLOCKED

    signoff = sc.run_dir / "signoff.json"
    signoff.write_text("{}")
    (sc.run_dir / "cockpit.xlsx").write_bytes(b"old")
    result = sc.construction.revalidate(sc.run_id, STEWARD, DEDUP)

    assert result.readiness.state == GateState.READY_FOR_LOAD and result.readiness.reasons == []
    assert result.validation.quality_score == 100.0 and result.validation.defective_records == 0
    assert result.defects.resolved == 3 and result.defects.total_open == 0 and result.deduplicated_again
    assert result.signoff_revoked and not signoff.exists() and list(sc.run_dir.glob("signoff.revoked.*"))
    with session_factory() as s:  # the transformation counters now reflect the constructed data (found by the e2e run)
        run_row = s.get(MigMigrationRun, sc.run_id)
        assert (run_row.records_extracted, run_row.records_transformed, run_row.records_failed) == (300, 300, 0)
    assert pending(sc).activity[-1].detail["records_transformed"] == 300
    assert not (sc.run_dir / "cockpit.xlsx").exists() and list(sc.run_dir.glob("cockpit.stale.*"))
    assert DefectService(session_factory).get_defects(sc.run_id, open_only=True) == []
    assert len(DefectService(session_factory).get_defects(sc.run_id)) == 3  # the rows stay, at zero (audit trail)
    assert sc.governance.evaluate_readiness(sc.run_id).state == GateState.READY_FOR_LOAD
    assert list((sc.run_dir / "validation_history").glob("*.json"))
    assert pending(sc).status == "NOT_REQUIRED"
    assert [a.event for a in pending(sc).activity] == ["template", "upload", "revalidate"]
    assert pending(sc).activity[-1].detail["gate"] == "READY_FOR_LOAD"
    with pytest.raises(ConstructionStateError):  # nothing left to construct
        sc.construction.generate_construction_template(sc.run_id, tmp_path / "again.xlsx")


# ------------------------------------------------------------------ guards
def test_uploads_are_rejected_when_they_do_not_match_the_run(scenario, tmp_path):
    sc = scenario
    template = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx").path

    def mutate(edit, name):
        wb = load_workbook(template)
        edit(wb)
        out = tmp_path / name
        wb.save(out)
        return out

    def ingest(path):
        return sc.construction.ingest_constructed_data(sc.run_id, path, STEWARD)

    def col(ws, header):
        return [c.value for c in ws[1]].index(header) + 1

    # SOURCE_PK that does not match the staging record at that ROW_REF
    def swap_pk(wb):
        ws = wb[DATA_SHEET]
        ws.cell(row=2, column=col(ws, "SOURCE_PK")).value = "999999"
    with pytest.raises(ConstructionInputError, match="does not match staging record"):
        ingest(mutate(swap_pk, "pk.xlsx"))

    def bad_ref(wb):
        ws = wb[DATA_SHEET]
        ws.cell(row=2, column=col(ws, "ROW_REF")).value = 2
    with pytest.raises(ConstructionInputError, match="not in this template"):
        ingest(mutate(bad_ref, "ref.xlsx"))

    def dup_ref(wb):
        ws = wb[DATA_SHEET]
        ws.cell(row=3, column=col(ws, "ROW_REF")).value = ws.cell(row=2, column=col(ws, "ROW_REF")).value
    with pytest.raises(ConstructionInputError, match="more than once"):
        ingest(mutate(dup_ref, "dup.xlsx"))

    def rename_header(wb):
        wb[DATA_SHEET].cell(row=1, column=col(wb[DATA_SHEET], "AKONT")).value = "AKONT2"
    with pytest.raises(ConstructionInputError, match="missing or renamed"):
        ingest(mutate(rename_header, "hdr.xlsx"))

    def no_meta(wb):
        del wb[META_SHEET]
    with pytest.raises(ConstructionInputError, match="sheets of the template"):
        ingest(mutate(no_meta, "meta.xlsx"))

    def empty(wb):
        ws = wb[DATA_SHEET]
        ws.delete_rows(2, ws.max_row)
    with pytest.raises(ConstructionInputError, match="no data rows"):
        ingest(mutate(empty, "empty.xlsx"))

    not_xlsx = tmp_path / "x.xlsx"
    not_xlsx.write_text("this is not a workbook")
    with pytest.raises(ConstructionInputError, match="not a valid .xlsx"):
        ingest(not_xlsx)
    with pytest.raises(ValueError):
        sc.construction.ingest_constructed_data(sc.run_id, template, "  ")
    assert constructed_lineage(sc.ctx.session_factory, sc.run_id) == []  # nothing was merged by any of the above
    assert not (sc.run_dir / "construction" / "staging.parquet").exists()


def test_formulas_and_unrequested_cells_are_rejected_not_merged(scenario, tmp_path):
    sc = scenario
    template = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx").path
    wb = load_workbook(template)
    ws = wb[DATA_SHEET]
    headers = {c.value: c.column for c in ws[1]}
    done_formula = done_locked = False
    for r in range(2, ws.max_row + 1):
        for h in ("BU_GROUP", "AKONT", "COUNTRY"):
            cell = ws.cell(row=r, column=headers[h])
            if cell.protection.locked is False and not done_formula:
                cell.value, cell.data_type, done_formula = "=1+1", "s", True  # a formula typed as text
            elif cell.protection.locked is True and not done_locked:
                cell.value, done_locked = "BP01", True  # a cell that was not requested for this record
    out = tmp_path / "f.xlsx"
    wb.save(out)
    result = sc.construction.ingest_constructed_data(sc.run_id, out, STEWARD)
    assert result.validation_status == "REJECTED" and result.cells_modified == 0 and result.lineage_rows == 0
    assert {c.reason for c in result.rejected_samples} == {"formulas are not allowed", "not requested for this record"}
    assert not (sc.run_dir / "construction" / "staging.parquet").exists()


def test_old_and_foreign_templates_are_refused(scenario, tmp_path):
    sc = scenario
    first = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t1.xlsx").path
    sc.construction.generate_construction_template(sc.run_id, tmp_path / "t2.xlsx")
    with pytest.raises(ConstructionInputError, match="superseded"):
        sc.construction.ingest_constructed_data(sc.run_id, fill(first, tmp_path / "f.xlsx", good_values), STEWARD)

    wb = load_workbook(tmp_path / "t2.xlsx")
    wb[META_SHEET]["B2"] = sc.run_id + 1
    wb.save(tmp_path / "other_run.xlsx")
    with pytest.raises(ConstructionInputError, match="belongs to run"):
        sc.construction.ingest_constructed_data(sc.run_id, tmp_path / "other_run.xlsx", STEWARD)

    wb = Workbook()
    wb.active.title = "Sheet1"
    wb.save(tmp_path / "blank.xlsx")
    with pytest.raises(ConstructionInputError):
        sc.construction.ingest_constructed_data(sc.run_id, tmp_path / "blank.xlsx", STEWARD)


def test_truncated_template_only_accepts_its_own_rows(scenario, tmp_path):
    sc = scenario
    first = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx", max_rows=10)
    r = sc.construction.ingest_constructed_data(sc.run_id, fill(first.path, tmp_path / "f.xlsx", good_values), STEWARD)
    assert r.records_updated == 10 and r.remaining_cells_pending == 0  # pending is counted against this template
    second = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t2.xlsx")  # the other 40 records
    assert second.records == 40 and not second.truncated


def test_state_guards_unknown_runs_and_loaded_runs(scenario, session_factory, tmp_path):
    sc = scenario
    with pytest.raises(NotFoundError):
        sc.construction.generate_construction_template(999_999, tmp_path / "x.xlsx")
    with pytest.raises(ConstructionStateError, match="no constructed data"):
        sc.construction.revalidate(sc.run_id, STEWARD)
    with pytest.raises(ConstructionStateError, match="download one first"):
        sc.construction.ingest_constructed_data(sc.run_id, tmp_path / "nothing.xlsx", STEWARD)
    template = sc.construction.generate_construction_template(sc.run_id, tmp_path / "t.xlsx").path
    with session_factory.begin() as s:
        s.execute(sa.update(MigMigrationRun).where(MigMigrationRun.id == sc.run_id).values(current_phase=MigrationPhase.LOAD))
    for call in (lambda: sc.construction.generate_construction_template(sc.run_id, tmp_path / "y.xlsx"),
                 lambda: sc.construction.ingest_constructed_data(sc.run_id, template, STEWARD),
                 lambda: sc.construction.revalidate(sc.run_id, STEWARD)):
        with pytest.raises(ConstructionStateError, match="not been loaded"):
            call()
    assert pending(sc).status == "LOCKED" and not pending(sc).can_construct


# ------------------------------------------------------------------ API: download -> upload -> revalidate
def test_api_construction_workflow(scenario, tmp_path):
    sc, client = scenario, scenario.client
    summary = client.get(f"/api/construction/{sc.run_id}").json()
    assert summary["status"] == "PENDING" and summary["incomplete_records"] == 50
    assert {f["target"] for f in summary["required_fields"]} == {"BUT000.BU_GROUP", "KNB1.AKONT", "ADRC.COUNTRY"}

    anonymous = TestClient(client.app).get(f"/api/construction/template/{sc.run_id}")
    assert anonymous.status_code == 401
    download = client.get(f"/api/construction/template/{sc.run_id}")
    assert download.status_code == 200 and "spreadsheetml" in download.headers["content-type"]
    assert download.headers["x-template-records"] == "50" and download.headers["x-template-truncated"] == "false"
    assert "construction_template_run_" in download.headers["content-disposition"]
    template = tmp_path / "from_api.xlsx"
    template.write_bytes(download.content)

    filled = fill(template, tmp_path / "filled.xlsx", good_values)
    wrong_type = client.post(f"/api/construction/upload/{sc.run_id}", files={"file": ("x.csv", b"a,b", "text/csv")})
    assert wrong_type.status_code == 422
    garbage = client.post(f"/api/construction/upload/{sc.run_id}", files={"file": ("x.xlsx", b"nope", "application/octet-stream")})
    assert garbage.status_code == 422 and garbage.json()["code"] == "INVALID_WORKBOOK"
    assert client.post("/api/construction/upload/999999", files={"file": ("x.xlsx", b"nope", "x")}).status_code == 404

    up = client.post(f"/api/construction/upload/{sc.run_id}", files={"file": ("filled.xlsx", filled.read_bytes(), "application/octet-stream")})
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["records_updated"] == 50 and body["cells_modified"] == EXPECTED_CELLS and body["validation_status"] == "OK"
    assert body["uploaded_by"] == STEWARD and body["revalidation_required"] and body["remaining_cells_pending"] == 0

    mid = client.get(f"/api/construction/{sc.run_id}").json()
    assert mid["status"] == "REVALIDATION_REQUIRED"
    assert [a["event"] for a in mid["activity"]] == ["template", "upload"] and mid["activity"][1]["user"] == STEWARD
    assert client.get(f"/api/defects/{sc.run_id}").json()["readiness"]["state"] == "BLOCKED"

    rv = client.post(f"/api/construction/revalidate/{sc.run_id}")
    assert rv.status_code == 200, rv.text
    r = rv.json()
    assert r["readiness"]["state"] == "READY_FOR_LOAD" and r["validation"]["quality_score"] == 100.0
    assert r["defects"]["resolved"] == 3 and r["defects"]["total_open"] == 0
    gate = client.get(f"/api/defects/{sc.run_id}").json()
    assert gate["open_defects"] == 0 and gate["quality_score"] == 100.0 and gate["readiness"]["state"] == "READY_FOR_LOAD"
    done = client.get(f"/api/construction/{sc.run_id}").json()
    assert done["status"] == "NOT_REQUIRED" and [a["event"] for a in done["activity"]] == ["template", "upload", "revalidate"]
    assert client.get(f"/api/construction/template/{sc.run_id}").status_code == 409  # nothing left to construct
    assert client.get(f"/api/construction/template/{sc.run_id}?max_rows=0").status_code == 422


# ------------------------------------------------------------------ fields with no rule at all
def test_missing_fields_without_rules_use_placeholder_rules_and_rededuplication(service, session_factory, tmp_path):
    ctx = AppContext(Settings(workspace_dir=tmp_path / "ws", auto_seed=False), session_factory)
    client = TestClient(create_app(ctx), headers={"X-User": "alice"})
    seed_sap_catalog(service)
    rows = build_rows()
    for r in rows:
        r["GROUP_CD"], r["RECON_ACCT"] = "BP01", "113100"
    reg = client.post("/api/sources/register", data={"system_name": "LEGACY_AR", "system_type": "ORACLE", "table_name": "AR_CUSTOMERS",
                                                     "primary_key_columns": "CUST_ID"},
                      files={"file": ("a.csv", write_csv(tmp_path / "a.csv", rows).read_bytes(), "text/csv")}).json()
    table_id = reg["table_id"]
    but000 = next(t.table for t in service.get_target_dictionary().tables if t.table.table_name == "BUT000")
    mapping = service.create_mapping_set(MappingSetCreate(source_table_id=table_id, target_table_id=but000.id, created_by=AUTHOR))
    mid = mapping.id
    for payload in ({"target_table": "BUT000", "target_column": "BP_EXT", "rule_type": "SQL_EXPRESSION", "source_column": "CUST_ID",
                     "transformation_logic": "LPAD(TRIM(CUST_ID), 10, '0')"},
                    {"target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "DIRECT_COPY", "source_column": "CUST_NAME"},
                    {"target_table": "ADRC", "target_column": "CITY1", "rule_type": "DIRECT_COPY", "source_column": "CITY"},
                    {"target_table": "ADRC", "target_column": "COUNTRY", "rule_type": "DIRECT_COPY", "source_column": "COUNTRY"},
                    {"target_table": "BUT000", "target_column": "TYPE", "rule_type": "STATIC_VALUE", "transformation_logic": "'2'"}):
        assert client.post(f"/api/mapping/{mid}/rules", json=payload).status_code == 201

    placeholders = client.post(f"/api/construction/placeholder-rules/{mid}")
    assert placeholders.status_code == 201
    # key fields that must be supplied (company code, sales area) are included; generated identifiers are not
    assert {p["target"] for p in placeholders.json()} == {
        "BUT000.BU_GROUP", "KNB1.BUKRS", "KNB1.AKONT", "KNVV.VKORG", "KNVV.VTWEG", "KNVV.SPART", "KNVV.WAERS"}
    assert client.post(f"/api/construction/placeholder-rules/{mid}").json() == []  # idempotent
    detail = client.get(f"/api/mapping/{mid}").json()
    assert detail["unmapped_mandatory_targets"] == []
    assert {r["rule_type"] for r in detail["rules"] if r["target_column"] in ("BU_GROUP", "AKONT", "WAERS", "BUKRS", "VKORG")} == {"STATIC_VALUE"}

    client.post(f"/api/governance/submit-mapping/{mid}")
    assert client.post(f"/api/governance/approve-mapping/{mid}", headers={"X-User": STEWARD}).status_code == 200
    assert client.post(f"/api/construction/placeholder-rules/{mid}").status_code == 409  # approved mapping sets are frozen

    run = client.post(f"/api/pipeline/run/{mid}", json={}).json()
    run_id = run["run_id"]
    assert run["readiness"]["state"] == "BLOCKED" and run["dedup_applied"]  # every record misses 3 mandatory fields
    assert run["run"]["deduplication"]["duplicate_children"] == 0 and run["run"]["deduplication"]["excluded_failed_rows"] == 300
    assert (ctx.run_dir(run_id) / "dedup_config.json").is_file()
    summary = client.get(f"/api/construction/{run_id}").json()
    assert summary["incomplete_records"] == 300
    assert {f["target"]: f["records"] for f in summary["required_fields"]} == {
        "BUT000.BU_GROUP": 300, "KNB1.BUKRS": 300, "KNB1.AKONT": 300, "KNVV.VKORG": 300, "KNVV.VTWEG": 300,
        "KNVV.SPART": 300, "KNVV.WAERS": 300, "ADRC.COUNTRY": EXPECTED_FIELD_COUNTS["COUNTRY"]}

    template = tmp_path / "t.xlsx"
    template.write_bytes(client.get(f"/api/construction/template/{run_id}").content)
    values = {"BU_GROUP": "BP01", "AKONT": "113100", "WAERS": "USD", "COUNTRY": "DE", "BUKRS": "1000", "VKORG": "1010", "VTWEG": "10", "SPART": "00"}
    filled = fill(template, tmp_path / "f.xlsx", lambda h, row: values[h])
    up = client.post(f"/api/construction/upload/{run_id}", files={"file": ("f.xlsx", filled.read_bytes(), "x")}).json()
    assert up["records_updated"] == 300 and up["cells_modified"] == 7 * 300 + EXPECTED_FIELD_COUNTS["COUNTRY"] and up["validation_status"] == "OK"

    rv = client.post(f"/api/construction/revalidate/{run_id}").json()
    assert rv["deduplicated_again"] and rv["readiness"]["state"] == "READY_FOR_LOAD"
    # deduplication now covers the records that were excluded as failed before construction
    assert rv["validation"]["input_records"] == 300 and rv["validation"]["evaluated_records"] == 300
    assert all(r["_TRANSFORM_STATUS"] == "OK" for r in read_parquet(ctx.run_dir(run_id) / "construction" / "deduplicated.parquet"))

    # the cockpit's company-code and sales-area sheets are now populated (they were empty when the key fields were skipped)
    from src.services.cockpit_generator import COMPANY, SALES, CockpitGenerator
    cockpit = CockpitGenerator().generate(rv["validation"]["valid_path"], tmp_path / "cockpit.xlsx")
    valid = rv["validation"]["valid_records"]
    assert cockpit.sheet_rows[COMPANY] == valid and cockpit.sheet_rows[SALES] == valid and cockpit.warnings == []


def test_construction_endpoints_are_in_the_api_schema(scenario):
    paths = scenario.client.get("/openapi.json").json()["paths"]
    assert "get" in paths["/api/construction/template/{run_id}"] and "post" in paths["/api/construction/upload/{run_id}"]
    assert "post" in paths["/api/construction/revalidate/{run_id}"]
    assert Severity.CRITICAL.value == "CRITICAL" and io.BytesIO and ColumnCreate and ValidationEngine  # imports used


def test_governance_hub_has_the_data_construction_tab(scenario):
    page = scenario.client.get(f"/ui/governance/{scenario.run_id}")
    assert page.status_code == 200
    for text in ("Data Construction", "Download Construction Template (.xlsx)", "Upload Completed Template (.xlsx)",
                 "construction-cards", "id=\"dropzone\"",
                 "Activity log", 'accept=".xlsx"', "Re-validate"):
        assert text in page.text, text
    assert "/static/js/construction.js" in page.text and 'role="tablist"' in page.text
    assert "<script>" not in page.text.replace("<script src", "")  # no inline script (CSP)
    js = scenario.client.get("/static/js/construction.js")
    assert js.status_code == 200
    for needle in ("Total incomplete records", "Required target fields", "/api/construction/upload/", "/api/construction/revalidate/",
                   "/api/construction/template/", "activity", "drop"):
        assert needle in js.text, needle
    assert "dropzone" in scenario.client.get("/static/css/app.css").text
