"""The destination a mapping set is pointed at (see mapping_targets.py)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import MappingTargetIn, MappingTargetOut
from src.connectors.dialects import CONNECTORS
from src.db.models import MapTableRule

router = APIRouter(tags=["mapping"])


def _out(ctx: AppContext, mapping_set_id: int) -> MappingTargetOut:
    entry = ctx.mapping_targets.record(mapping_set_id)
    if not entry:
        return MappingTargetOut(mapping_set_id=mapping_set_id, connection=None, label="SAP S/4HANA (default)")
    record = ctx.connections.get(entry["connection"])
    cls = CONNECTORS.get(record.dialect) if record else None
    return MappingTargetOut(
        mapping_set_id=mapping_set_id, connection=entry["connection"], label=f"{cls.label} ({entry['connection']})" if cls else entry["connection"],
        dialect=record.dialect if record else None, default_schema=record.default_schema if record else None, set_by=entry["set_by"], set_at=entry["set_at"],
        problem=None if record else f"the connection '{entry['connection']}' no longer exists")


def _mapping(ctx: AppContext, mapping_set_id: int) -> MapTableRule:
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise HTTPException(404, f"mapping set {mapping_set_id} does not exist")
        s.expunge(mapping)
        return mapping


@router.get("/mapping/{mapping_set_id}/target", response_model=MappingTargetOut)
def get_target(mapping_set_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> MappingTargetOut:
    """Where this mapping set's result is meant to go: a saved target connection, or the default (SAP)."""
    _mapping(ctx, mapping_set_id)
    return _out(ctx, mapping_set_id)


@router.put("/mapping/{mapping_set_id}/target", response_model=MappingTargetOut)
def set_target(mapping_set_id: int, body: MappingTargetIn, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> MappingTargetOut:
    """Point a DRAFT mapping set at a target connection (so the pre-submission check applies that destination's rules), or back at the default with `connection: null`."""
    mapping = _mapping(ctx, mapping_set_id)
    if str(getattr(mapping.status, "value", mapping.status)) not in ("DRAFT", "REJECTED"):
        raise HTTPException(409, "only a draft mapping set can change its destination: an approved one has been reviewed with the destination it had. Create a new version instead.")
    if body.connection:
        record = ctx.connections.get(body.connection)
        if record is None:
            raise HTTPException(404, f"connection '{body.connection}' does not exist")
        if record.role != "target":
            raise HTTPException(409, f"connection '{body.connection}' is a source connection (read-only); choose a target connection")
    ctx.mapping_targets.set(mapping_set_id, body.connection, user)
    return _out(ctx, mapping_set_id)
