"""Mapping-set approval (segregation of duties) and the final load sign-off."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import MappingSetSummary, SignOffResponse
from src.api.views import mapping_summary
from src.db.models import MapTableRule, MigMigrationRun
from src.services.governance_service import GateBlockedError, NotAuthorizedError, SegregationOfDutiesError

router = APIRouter(prefix="/governance", tags=["governance"])


def _summary(ctx: AppContext, mapping_set_id: int) -> MappingSetSummary:
    with ctx.session_factory() as s:
        return mapping_summary(s, s.get(MapTableRule, mapping_set_id))


@router.post("/submit-mapping/{mapping_set_id}", response_model=MappingSetSummary)
def submit_mapping(mapping_set_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)):
    """DRAFT -> SUBMITTED (hands the mapping set to a steward for review)."""
    ctx.governance.submit_mapping_set(mapping_set_id, user)
    return _summary(ctx, mapping_set_id)


@router.post("/approve-mapping/{mapping_set_id}", response_model=MappingSetSummary)
def approve_mapping(mapping_set_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)):
    """SUBMITTED -> APPROVED. The approver is the authenticated caller and may not be the mapping's
    creator (403 SEGREGATION_OF_DUTIES); the database CHECK constraint backs this up."""
    ctx.governance.approve_mapping_set(mapping_set_id, user)
    return _summary(ctx, mapping_set_id)


@router.post("/reject-mapping/{mapping_set_id}", response_model=MappingSetSummary)
def reject_mapping(mapping_set_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)):
    """SUBMITTED -> REJECTED."""
    ctx.governance.reject_mapping_set(mapping_set_id, user)
    return _summary(ctx, mapping_set_id)


@router.post("/sign-off-load/{run_id}", response_model=SignOffResponse)
def sign_off_load(run_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> SignOffResponse:
    """Final gate check. Succeeds only when the run is READY_FOR_LOAD (409 GATE_BLOCKED otherwise),
    and records the steward's authorisation that the load endpoint then requires."""
    readiness = ctx.governance.evaluate_readiness(run_id)
    if not readiness.ready:
        raise GateBlockedError(readiness)
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, s.get(MigMigrationRun, run_id).mapping_set_id)
        creator = mapping.created_by
    approvers = ctx.settings.approvers
    if approvers is not None and user.casefold() not in {a.casefold() for a in approvers}:
        raise NotAuthorizedError(f"'{user}' is not an authorised approver")
    if user.strip().casefold() == creator.strip().casefold():
        raise SegregationOfDutiesError(f"'{user}' created the mapping set and cannot sign off its load")

    signed_at = datetime.now(timezone.utc)
    directory = ctx.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "signoff.json").write_text(
        json.dumps({"run_id": run_id, "signed_off_by": user, "signed_off_at": signed_at.isoformat()}, indent=2),
        encoding="utf-8")
    return SignOffResponse(run_id=run_id, signed_off_by=user, signed_off_at=signed_at, readiness=readiness)
