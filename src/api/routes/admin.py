"""Health and catalog bootstrap."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import HealthResponse
from src.db.enums import TargetType
from src.db.models import CatDataSource, CatTable
from src.seeds.seed_sap_catalog import seed_sap_catalog

router = APIRouter(tags=["admin"])
public_router = APIRouter(tags=["admin"])  # reachable without credentials (probes)


def target_catalog_ready(ctx: AppContext) -> bool:
    with ctx.session_factory() as s:
        n = s.scalar(select(func.count()).select_from(CatTable).join(CatDataSource, CatTable.source_id == CatDataSource.id)
                     .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == "BUSINESS_PARTNER"))
        return bool(n)


@public_router.get("/health", response_model=HealthResponse)
def health(ctx: AppContext = Depends(get_ctx)) -> HealthResponse:
    try:
        with ctx.session_factory() as s:
            s.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 - health must report, not raise
        database = "unreachable"
    return HealthResponse(
        status="ok" if database == "ok" else "degraded", database=database, auth_mode=ctx.settings.auth_mode,
        jira_mode="dry-run" if ctx.jira.dry_run else "live", sap_default_target="sandbox",
        workspace=ctx.settings.workspace_dir.name,
        target_catalog_ready=target_catalog_ready(ctx) if database == "ok" else False)


@router.post("/admin/seed-sap-catalog")
def seed_catalog(ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict:
    """Idempotently (re)seed the SAP S/4HANA Business Partner target catalog."""
    result = seed_sap_catalog(ctx.metadata)
    return {"source_id": result.source_id, "tables": result.tables, "columns": result.columns}
