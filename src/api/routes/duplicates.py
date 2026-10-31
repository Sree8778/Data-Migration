"""Review of possible duplicates: list what needs a person, record the decisions, see what was decided. Decisions apply from the next run of the same table."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.db.models import MapTableRule, MigMigrationRun
from src.services.duplicate_review import ReviewError
from src.services.metadata_service import NotFoundError

router = APIRouter(prefix="/duplicates", tags=["duplicates"])


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pk_a: str = Field(min_length=1, max_length=255)
    pk_b: str = Field(min_length=1, max_length=255)
    decision: Literal["MERGE", "DISTINCT", "CLEAR"]
    reason: Optional[str] = Field(default=None, max_length=300)


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[Decision] = Field(min_length=1, max_length=500)


def _table_of_run(ctx: AppContext, run_id: int) -> int:
    with ctx.session_factory() as s:
        run = s.get(MigMigrationRun, run_id)
        if run is None:
            raise NotFoundError(f"migration run {run_id} does not exist")
        return s.get(MapTableRule, run.mapping_set_id).source_table_id


@router.get("/{run_id}/possible")
def possible_duplicates(run_id: int, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), undecided_only: bool = False, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Pairs that look alike but not strongly enough to merge (best match first). Nothing was merged: both records load unless somebody decides MERGE."""
    return ctx.duplicate_review.possible(ctx.run_dir(run_id), _table_of_run(ctx, run_id), limit, offset, undecided_only)


@router.get("/{run_id}/merges")
def merged_records(run_id: int, strength: Optional[Literal["NAME_ONLY", "NAME_ADDRESS", "EXACT", "REVIEWED"]] = "NAME_ONLY", limit: int = Query(100, ge=1, le=1000),
                   offset: int = Query(0, ge=0), ctx: AppContext = Depends(get_ctx)) -> dict:
    """Records this run merged away, with the record each was merged into. By default the ones merged on the name alone: wrong merges are most likely there."""
    return ctx.duplicate_review.merges(ctx.run_dir(run_id), _table_of_run(ctx, run_id), strength, limit, offset)


@router.get("/{run_id}/decisions")
def decisions(run_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Every decision taken about records of this run's source table (they apply to every run of the table)."""
    data = ctx.duplicate_review.decisions(_table_of_run(ctx, run_id))
    return {"total": len(data), "decisions": sorted(data.values(), key=lambda d: d["at"], reverse=True)}


@router.post("/{run_id}/decide")
def decide(run_id: int, body: DecideRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict:
    """MERGE = these two are one entity; DISTINCT = they are different, never merge them; CLEAR = withdraw the decision. Decisions are kept for the source table and applied by
    every later run (and by re-deduplication after data construction): run the pipeline again to see their effect."""
    table_id = _table_of_run(ctx, run_id)
    try:
        counts = ctx.duplicate_review.decide(table_id, run_id, [d.model_dump() for d in body.decisions], user)
    except ReviewError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"recorded": counts, "applies_from": "the next run of this table", "decided_by": user}
