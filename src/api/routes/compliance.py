"""Compliance: the audit trail, the live control self-assessment, the record of processing, and the retention / erasure / legal-hold tools.

Reading is for the auditor role; changing anything (erasure, hold, sweep) is for admins (see api/security.POLICY). Every call here is itself on the audit trail.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.services.compliance import ComplianceReport, build_report, processing_record
from src.services.retention import RetentionError

router = APIRouter(prefix="/compliance", tags=["compliance"])


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


@router.get("/report", response_model=ComplianceReport)
def compliance_report(request: Request, ctx: AppContext = Depends(get_ctx)) -> ComplianceReport:
    """Every control the application relies on, checked now against this installation, with the standards it serves. Evidence, not certification."""
    return build_report(ctx, _https(request))


@router.get("/processing-record")
def record_of_processing(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The technical part of a GDPR art. 30 record: categories of personal data, purpose, recipients, storage, retention and measures."""
    return processing_record(ctx)


# ------------------------------------------------------------------ audit trail
@router.get("/audit-log")
def audit_log(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), user: Optional[str] = None, path: Optional[str] = None,
              outcome: Optional[str] = None, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The newest entries of the audit trail first. Filters: who, a part of the path, the outcome (ok, denied, rejected, error, throttled)."""
    entries, total = ctx.audit.read(limit, offset, user, path, outcome)
    return {"total": total, "entries": entries}


@router.get("/audit-log/head")
def audit_head(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Where the chain ends now. Copy it to a system the application cannot write (a SIEM, a WORM bucket) on a schedule: it is the only proof that nothing was cut off."""
    return {**ctx.audit.head(), "keyed": ctx.audit.keyed}


@router.get("/audit-log/verify")
def audit_verify(expected_seq: Optional[int] = None, expected_hash: Optional[str] = None, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Re-compute the whole chain; with the head you anchored earlier it also proves that nothing after it was removed."""
    head = {"seq": expected_seq, "hash": expected_hash} if expected_seq is not None and expected_hash else None
    return ctx.audit.verify(head)


# ------------------------------------------------------------------ retention, erasure, legal hold
@router.post("/retention/sweep")
def retention_sweep(dry_run: bool = True, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Runs older than MIGRATION_RETENTION_DAYS whose record values are still on disk. `dry_run=true` (the default) only lists them; runs under legal hold are skipped."""
    days = ctx.settings.retention_days
    if not days:
        raise HTTPException(409, "no retention period is set: set MIGRATION_RETENTION_DAYS")
    try:
        return ctx.retention.sweep(days, user, dry_run)
    except RetentionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/runs/{run_id}/erase")
def erase_run(run_id: int, body: ReasonBody, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Remove every record value of a run (staging, rejected, duplicates, workbooks, before-images, cell lineage); counts, rules, hashes, sign-offs and
    delivery records stay as evidence. Refused while the run is running or under legal hold."""
    try:
        return ctx.retention.erase_run(run_id, user, body.reason)
    except RetentionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tables/{table_id}/erase")
def erase_table(table_id: int, body: ReasonBody, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Remove the landed copy of a legacy table (snapshot data, the original file, profile) and the sample values in the catalog; the checksum and counts stay."""
    try:
        return ctx.retention.erase_table(table_id, user, body.reason)
    except RetentionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/maintenance/run")
def run_maintenance_now(ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Run the maintenance job now (the same one MIGRATION_MAINTENANCE_HOURS schedules): erase runs past the retention period (not those under legal hold) and remind the
    webhook about overdue tasks."""
    return ctx.run_maintenance(f"{user} (maintenance)")


@router.post("/classify-pii")
def classify_existing_columns(ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Apply the personal-data detector to every source column already in the catalog: raises classifications (never lowers one) and masks the sample values and
    profile screens that were stored raw before automatic classification existed. Safe to repeat."""
    return ctx.retention.classify_existing()


@router.post("/runs/{run_id}/hold")
def hold_run(run_id: int, body: ReasonBody, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Legal hold: the run's data cannot be erased, by hand or by the sweep, until the hold is released."""
    try:
        return ctx.retention.hold(run_id, user, body.reason)
    except RetentionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/runs/{run_id}/release")
def release_run(run_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    return {"run_id": run_id, "released": ctx.retention.release(run_id), "released_by": user}
