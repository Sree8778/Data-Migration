"""Data Construction endpoints: Excel template out, completed template in, re-validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.routes.sources import _store_upload
from src.schemas.construction import (
    ConstructionResult,
    ConstructionSummary,
    PlaceholderRule,
    RevalidationResult,
)
from src.schemas.execution import DedupConfig
from src.services.construction_service import DEFAULT_MAX_ROWS

router = APIRouter(prefix="/construction", tags=["construction"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/template/{run_id}")
def download_template(
    run_id: int, max_rows: int = DEFAULT_MAX_ROWS, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
):
    """Generate and download the construction workbook for a validated, not yet loaded run.

    Each download creates a new template version; earlier downloads are then rejected on upload."""
    if not 1 <= max_rows <= 100_000:
        raise HTTPException(422, "max_rows must be between 1 and 100000")
    result = ctx.construction.generate_construction_template(run_id, max_rows=max_rows, user=user)
    headers = {"X-Template-Records": str(result.records), "X-Template-Truncated": str(result.truncated).lower()}
    return FileResponse(result.path, media_type=XLSX, filename=f"construction_template_run_{run_id}.xlsx", headers=headers)


@router.post("/upload/{run_id}", response_model=ConstructionResult)
def upload_completed_template(
    run_id: int, file: UploadFile = File(...), ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
) -> ConstructionResult:
    """Merge a completed workbook: valid cells are applied (with lineage), invalid cells are reported."""
    if Path(file.filename or "").suffix.lower() != ".xlsx":
        raise HTTPException(422, "upload the completed template as an .xlsx file")
    scratch = ctx.workspace / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        target = Path(tmp) / "upload.xlsx"
        _store_upload(file, target, ctx.settings.max_upload_mb)
        return ctx.construction.ingest_constructed_data(run_id, target, user, filename=file.filename)


@router.post("/revalidate/{run_id}", response_model=RevalidationResult)
def revalidate(run_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> RevalidationResult:
    """Re-run pre-load validation on the merged data, refresh the defects and the gate.

    Deduplication is repeated with the run's original settings when they are known. An earlier load
    sign-off is revoked, because the data it approved has changed."""
    config_file = ctx.run_dir(run_id) / "dedup_config.json"
    dedup = DedupConfig.model_validate_json(config_file.read_text(encoding="utf-8")) if config_file.is_file() else None
    return ctx.construction.revalidate(run_id, user, dedup)


@router.post("/placeholder-rules/{mapping_set_id}", response_model=list[PlaceholderRule], status_code=201)
def add_placeholder_rules(mapping_set_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)):
    """Add 'to be constructed' rules for mandatory SAP fields that have no rule (DRAFT mapping sets only)."""
    return ctx.construction.add_placeholder_rules(mapping_set_id, user)


@router.get("/{run_id}", response_model=ConstructionSummary)
def summary(run_id: int, ctx: AppContext = Depends(get_ctx)) -> ConstructionSummary:
    """Incomplete records, required fields, status and the activity log (for the Governance Hub)."""
    return ctx.construction.get_summary(run_id)
