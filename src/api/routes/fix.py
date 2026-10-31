"""One-click fixes (see src/api/remedies.py): the buttons for a mapping's problems, and the action behind each button."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api import remedies
from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.views import run_preflight
from src.schemas.preflight import PreflightReport
from src.schemas.remedy import FixRequest, FixResult, Remedy

router = APIRouter(tags=["fix"])


@router.get("/mapping/{mapping_set_id}/fixes", response_model=list[Remedy])
def fixes_for_mapping(mapping_set_id: int, stored: bool = False, ctx: AppContext = Depends(get_ctx)) -> list[Remedy]:
    """The 'Fix it' buttons for the problems of this mapping set: each says what exactly it will do before it is pressed. Based on a fresh pre-submission check, or on the
    stored one with ?stored=true (the screen has just run it)."""
    path = ctx.preflight_file(mapping_set_id)
    report = PreflightReport.model_validate_json(path.read_text(encoding="utf-8")) if stored and path.is_file() else run_preflight(ctx, mapping_set_id)
    return remedies.remedies_for_mapping(ctx, mapping_set_id, report)


@router.post("/mapping/fix", response_model=FixResult)
def fix(body: FixRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> FixResult:
    """Do what a 'Fix it' button said it would. On a mapping set that cannot change (approved, submitted, archived) an editable DRAFT copy is made first and the fix goes into
    the copy, which somebody else has to approve: a fix never approves itself and never alters what an earlier run was made with."""
    return remedies.execute(ctx, body, user)
