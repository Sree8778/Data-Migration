"""Defects, Jira sync, runs, reconciliation certificate and file downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import (
    DefectDetail,
    DefectsResponse,
    DeltaRequest,
    JiraSyncResponse,
    ReconciliationResponse,
    RunSummary,
)
from src.db.models import CatColumn, CatTable, MapFieldRule, MigMigrationRun
from src.schemas.delta import DeltaReport
from src.api.views import delta_report
from src.schemas.sap import CheckResult
from src.schemas.source import SnapshotInfo
from src.schemas.validation import ROOT_CAUSE_BY_KIND, Severity, kind_for_signature
from src.services.metadata_service import NotFoundError

router = APIRouter(tags=["audit"])


def _target_name(ctx: AppContext, field_rule_id: int) -> str:
    with ctx.session_factory() as s:
        rule = s.get(MapFieldRule, field_rule_id)
        col = s.get(CatColumn, rule.target_column_id)
        return f"{s.get(CatTable, col.table_id).table_name}.{col.column_name}"


@router.get("/defects/{run_id}", response_model=DefectsResponse)
def get_defects(run_id: int, ctx: AppContext = Depends(get_ctx)) -> DefectsResponse:
    """Rule-aggregated defects (most severe first) with Jira links, plus the live gate state."""
    readiness = ctx.governance.evaluate_readiness(run_id)  # 404 for unknown runs
    defects = ctx.defects.get_defects(run_id)
    validation = ctx.load_validation_report(run_id)
    details = []
    for d in defects:
        kind = kind_for_signature(d.rule_signature)
        details.append(DefectDetail(
            **d.model_dump(), target=_target_name(ctx, d.field_rule_id),
            root_cause=ROOT_CAUSE_BY_KIND[kind] if kind else "See the validation report."))
    return DefectsResponse(
        run_id=run_id, quality_score=validation.quality_score if validation else None,
        open_defects=sum(1 for d in defects if d.is_open),
        open_critical=sum(1 for d in defects if d.is_open and d.severity == Severity.CRITICAL),
        jira_mode="dry-run" if ctx.jira.dry_run else "live", defects=details, readiness=readiness)


@router.post("/defects/{run_id}/sync-jira", response_model=JiraSyncResponse)
def sync_jira(run_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> JiraSyncResponse:
    """Create/update one Jira issue per open defect (dry-run when Jira is not configured)."""
    ctx.governance.evaluate_readiness(run_id)
    keys = ctx.jira.sync_defects_to_jira(run_id)
    return JiraSyncResponse(run_id=run_id, dry_run=ctx.jira.dry_run, issue_keys=keys, errors=ctx.jira.last_errors)


# ------------------------------------------------------------------ runs
def _run_summary(ctx: AppContext, run: MigMigrationRun) -> RunSummary:
    validation = ctx.load_validation_report(run.id)
    return RunSummary(
        run_id=run.id, mapping_set_id=run.mapping_set_id, wave_name=run.wave_name, status=run.status.value,
        phase=run.current_phase.value, records_extracted=run.records_extracted,
        records_transformed=run.records_transformed, records_loaded=run.records_loaded,
        records_failed=run.records_failed, started_at=run.started_at, completed_at=run.completed_at,
        quality_score=validation.quality_score if validation else None)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(ctx: AppContext = Depends(get_ctx)) -> list[RunSummary]:
    with ctx.session_factory() as s:
        return [_run_summary(ctx, r) for r in s.scalars(select(MigMigrationRun).order_by(MigMigrationRun.id.desc()))]


@router.get("/runs/{run_id}", response_model=RunSummary)
def get_run(run_id: int, ctx: AppContext = Depends(get_ctx)) -> RunSummary:
    with ctx.session_factory() as s:
        run = s.get(MigMigrationRun, run_id)
        if run is None:
            raise NotFoundError(f"migration run {run_id} does not exist")
        return _run_summary(ctx, run)


@router.post("/runs/{run_id}/delta", response_model=DeltaReport)
def compute_delta(run_id: int, body: DeltaRequest = DeltaRequest(), ctx: AppContext = Depends(get_ctx)) -> DeltaReport:
    """What changed since an earlier run, on the valid records and the SAP target fields: new, changed, unchanged, and removed (with the
    reason). Every run is still validated against the whole population; the delta only says what is worth delivering again. The records
    to deliver are written to the run folder (delta_valid.parquet) and delivered with `mode: merge` on a database target."""
    return delta_report(ctx, run_id, body.baseline_run)


@router.get("/runs/{run_id}/delta", response_model=Optional[DeltaReport])
def get_delta(run_id: int, ctx: AppContext = Depends(get_ctx)) -> Optional[DeltaReport]:
    """The last delta computed for this run (null until one has been)."""
    path = ctx.run_dir(run_id) / "delta" / "delta.json"
    return DeltaReport.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def _add_extract_checks(report, snapshot_path) -> None:
    """Stage zero of the audit trail: the pipeline read exactly what was landed, and the landing matched the source."""
    if not snapshot_path.is_file():
        return
    snap = SnapshotInfo.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
    report.checks.append(CheckResult(
        name="extract_matches_landed_snapshot", passed=report.counts.extracted == snap.rows_landed,
        detail=f"pipeline extracted {report.counts.extracted} row(s); the {snap.format} snapshot holds {snap.rows_landed} "
               f"(file {snap.original_filename}, SHA-256 {snap.original_sha256[:12]}...)"))
    if snap.control_status == "MATCH":
        report.checks.append(CheckResult(
            name="source_control_total_verified", passed=snap.rows_landed == snap.expected_rows,
            detail=f"landed row count matches the {snap.expected_rows} row(s) reported by the source system"))


def _add_db_delivery_checks(report, run_dir) -> None:
    """Every delivery into a database target must have put exactly the validated records there (counted in the target itself)."""
    from src.services.db_loader_service import DbLoaderService
    latest = {}                                       # a table that was replaced holds only its latest delivery
    for d in DbLoaderService.deliveries(run_dir):
        latest[(d["connection"], d["schema"], d["table"])] = d
    for d in latest.values():
        if d.get("rolled_back"):
            rb = d["rolled_back"]
            report.checks.append(CheckResult(
                name=f"db_rollback_{d['dialect']}_{d['schema']}.{d['table']}", passed=rb["rows_removed"] == d["rows_loaded"],
                detail=f"delivery of {d['rows_loaded']} row(s) into {d['dialect']} {d['database']}:{d['schema']}.{d['table']} rolled back by {rb['by']} at {rb['at']} "
                       f"({rb['reason']}): {rb['rows_removed']} row(s) removed, {rb['rows_remaining']} left" + ("; table dropped" if rb["table_dropped"] else "")))
            continue
        if d.get("mode") == "merge":
            where_m = f"{d['dialect']} {d['database']}:{d['schema']}.{d['table']}"
            report.checks.append(CheckResult(
                name=f"db_merge_{d['dialect']}_{d['schema']}.{d['table']}",
                passed=d["rows_in_target"] == d["rows_before"] + d["inserted_new"] - d["deleted"] and d["rows_loaded"] == d["valid_records"],
                detail=f"delta of run {d['run_id']} against run {d['baseline_run']} applied to {where_m}: {d['inserted_new']} new, {d['updated']} replaced, "
                       f"{d['deleted']} deleted; the table held {d['rows_before']} row(s) before and {d['rows_in_target']} counted after the merge; "
                       f"{d['before_rows']} replaced or deleted row(s) kept in {d['before_image']} (merged by {d['loaded_by']} at {d['loaded_at']})"))
        else:
            report.checks.append(CheckResult(
                name=f"db_delivery_{d['dialect']}_{d['schema']}.{d['table']}",
                passed=d["rows_in_target"] == d["rows_loaded"] == d["valid_records"],
            detail=f"{d['rows_in_target']} row(s) counted in {d['dialect']} {d['database']}:{d['schema']}.{d['table']} after the load; "
                   f"{d['rows_loaded']} sent; {d['valid_records']} validated record(s) in the run (loaded by {d['loaded_by']} at {d['loaded_at']})"))
        where = f"{d['dialect']} {d['database']}:{d['schema']}.{d['table']}"
        if d.get("values_verified"):
            report.checks.append(CheckResult(
                name=f"db_values_{d['dialect']}_{d['schema']}.{d['table']}", passed=True,
                detail=f"{d['cells_compared']:,} cell(s) read back from {where} " + ("after writing, before the delivery was accepted," if d["dialect"] == "firestore" else "before the commit")
                       + f" equal what was sent (content fingerprint {d['value_digest']})"))
        elif "values_verified" in d:
            report.checks.append(CheckResult(
                name=f"db_values_{d['dialect']}_{d['schema']}.{d['table']}", passed=False,
                detail=f"the values delivered into {where} were NOT compared with what was sent (verification was switched off)"))
        if d.get("verifications"):
            last = d["verifications"][-1]
            report.checks.append(CheckResult(
                name=f"db_values_reverified_{d['dialect']}_{d['schema']}.{d['table']}", passed=bool(last["verified"]),
                detail=(f"on {last['checked_at']} {last['checked_by']} compared {last['cells_compared']:,} cell(s) of {where} with the validated payload: "
                        + ("identical" if last["verified"] else f"DIFFERENT in column(s) {', '.join(last['differing_columns']) or '(row counts differ)'}; "
                           f"payload {last['rows_in_payload']} row(s), table {last['rows_in_target']}"))))


# ------------------------------------------------------------------ reconciliation + downloads
@router.get("/reconciliation/{run_id}", response_model=ReconciliationResponse)
def get_reconciliation(run_id: int, ctx: AppContext = Depends(get_ctx)) -> ReconciliationResponse:
    """The reconciliation audit report (and Markdown/HTML certificate files) for a run."""
    directory = ctx.run_dir(run_id)
    report = ctx.reconciliation.generate_reconciliation_report(
        run_id, ctx.load_validation_report(run_id), ctx.exclusions.read_applied(directory))
    directory.mkdir(parents=True, exist_ok=True)
    _add_extract_checks(report, directory / "source_snapshot.json")
    _add_db_delivery_checks(report, directory)
    exports = ctx.reconciliation.export(report, directory)
    cockpit = directory / "cockpit.xlsx"
    return ReconciliationResponse(
        report=report, passed=report.passed,
        cockpit_download_url=f"/api/runs/{run_id}/cockpit" if cockpit.is_file() else None,
        exports=exports, export_urls={k: f"/api/reconciliation/{run_id}/download/{k}" for k in exports})


@router.get("/reconciliation/{run_id}/download/{fmt}")
def download_reconciliation(run_id: int, fmt: Literal["markdown", "html"], ctx: AppContext = Depends(get_ctx)):
    name = f"reconciliation_run_{run_id}." + ("md" if fmt == "markdown" else "html")
    path = ctx.run_dir(run_id) / name
    if not path.is_file():
        raise HTTPException(404, "generate the reconciliation report first (GET /api/reconciliation/{run_id})")
    return FileResponse(path, filename=name,
                        media_type="text/markdown" if fmt == "markdown" else "text/html")


@router.get("/runs/{run_id}/cockpit")
def download_cockpit(run_id: int, ctx: AppContext = Depends(get_ctx)):
    path = ctx.run_dir(run_id) / "cockpit.xlsx"
    if not path.is_file():
        raise HTTPException(404, f"no cockpit workbook generated for run {run_id}")
    return FileResponse(path, filename=f"migration_cockpit_run_{run_id}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
