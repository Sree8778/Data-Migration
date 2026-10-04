"""Defects, Jira sync, runs, reconciliation certificate and file downloads."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import (
    DefectDetail,
    DefectsResponse,
    JiraSyncResponse,
    ReconciliationResponse,
    RunSummary,
)
from src.db.models import CatColumn, CatTable, MapFieldRule, MigMigrationRun
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


# ------------------------------------------------------------------ reconciliation + downloads
@router.get("/reconciliation/{run_id}", response_model=ReconciliationResponse)
def get_reconciliation(run_id: int, ctx: AppContext = Depends(get_ctx)) -> ReconciliationResponse:
    """The reconciliation audit report (and Markdown/HTML certificate files) for a run."""
    report = ctx.reconciliation.generate_reconciliation_report(run_id, ctx.load_validation_report(run_id))
    directory = ctx.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
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
