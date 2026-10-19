"""Health and catalog bootstrap."""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import HealthResponse, TargetKind, TargetsResponse, TargetTestRequest, TargetTestResult
from src.services.sap_client import SapAuthError, SapConfig, SapError
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
        target_catalog_ready=target_catalog_ready(ctx) if database == "ok" else False,
        llm_provider=ctx.llm_provider,
        gemini_keys_active=sum(1 for k in ctx.gemini_keys.get_pool_status() if k["status"] == "ACTIVE"))


def _live_detail() -> tuple[bool, str]:
    config = SapConfig.from_env()
    if config is None:
        missing = [k for k in ("SAP_BASE_URL", "SAP_USER", "SAP_PASSWORD") if not os.environ.get(k, "").strip()]
        return False, "not configured on the server: set " + ", ".join(missing)
    host = urlparse(config.base_url).netloc or config.base_url
    return True, f"{host} as {config.user}" + (f", client {config.client}" if config.client else "")  # never the password


@router.get("/targets", response_model=TargetsResponse)
def list_targets(ctx: AppContext = Depends(get_ctx)) -> TargetsResponse:
    """The ways a run can be loaded, and whether each is ready. Credentials for the live system stay in the server's
    environment; this reports which are set, never their values."""
    live_ok, live_detail = _live_detail()
    from src.api.routes.connections import _out
    return TargetsResponse(object_name="BUSINESS_PARTNER", catalog_ready=target_catalog_ready(ctx),
                           databases=[_out(ctx, r) for r in ctx.connections.list() if r.role == "target"], targets=[
        TargetKind(kind="sandbox", label="SAP S/4HANA sandbox (simulated)", available=True, simulated=True, testable=True,
                   detail="Built-in simulator with SAP-style checks; hands out unique partner numbers. Safe for rehearsals."),
        TargetKind(kind="cockpit", label="SAP Migration Cockpit workbook", available=True, simulated=False, testable=False,
                   detail="Generates the staging workbook for upload in the SAP Migration Cockpit. No connection needed."),
        TargetKind(kind="live", label="SAP S/4HANA (OData API)", available=live_ok, simulated=False, testable=live_ok, detail=live_detail),
    ])


@router.post("/targets/test", response_model=TargetTestResult)
def test_target(body: TargetTestRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> TargetTestResult:
    """Check that a target can be reached and that the credentials work. Nothing is created in SAP."""
    started = time.monotonic()
    ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
    try:
        client, simulated = ctx.sap_client(body.target)
    except Exception as exc:  # noqa: BLE001 - e.g. live target not configured
        return TargetTestResult(target=body.target, ok=False, detail=str(exc), latency_ms=ms())
    if simulated:
        return TargetTestResult(target=body.target, ok=True, detail="The sandbox simulator is ready (nothing external is contacted).", latency_ms=ms())
    ping = getattr(client, "ping", None)
    if ping is None:
        return TargetTestResult(target=body.target, ok=False, detail="this SAP client does not support a connection test", latency_ms=ms())
    try:
        return TargetTestResult(target=body.target, ok=True, detail=ping(), latency_ms=ms())
    except SapAuthError as exc:
        return TargetTestResult(target=body.target, ok=False, detail=f"credentials rejected: {exc}", latency_ms=ms())
    except SapError as exc:
        return TargetTestResult(target=body.target, ok=False, detail=str(exc), latency_ms=ms())


@router.post("/admin/seed-sap-catalog")
def seed_catalog(ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict:
    """Idempotently (re)seed the SAP S/4HANA Business Partner target catalog."""
    result = seed_sap_catalog(ctx.metadata)
    return {"source_id": result.source_id, "tables": result.tables, "columns": result.columns}
