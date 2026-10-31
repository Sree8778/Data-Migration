"""Combine landed tables (joins and filters) into one new source table, with the measurement shown before anything is created."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.routes.sources import _commit, _scratch, read_snapshot
from src.api.schemas import SourceRegistration
from src.db.enums import SystemType
from src.db.models import CatTable
from src.schemas.combine import CombinePreview, CombinePreviewRequest, CombineRequest, CombineSpec
from src.schemas.source import LandingOptions
from src.services.source_landing import PARQUET_FILE, SourceLandingError, key_problems
from src.services.table_combiner import CombineError, TableCombiner

router = APIRouter(prefix="/sources/combine", tags=["sources"])
_combiner = TableCombiner()


def _inputs(ctx: AppContext, spec: CombineSpec) -> dict[int, tuple[str, Path]]:
    """table id -> (table name, landed Parquet) for every table the description uses."""
    ids = {spec.base.table_id, *(j.table_id for j in spec.joins)}
    with ctx.session_factory() as s:
        names = {t.id: t.table_name for t in s.scalars(select(CatTable).where(CatTable.id.in_(ids)))}
    out: dict[int, tuple[str, Path]] = {}
    for table_id in sorted(ids):
        if table_id not in names:
            raise HTTPException(404, f"table {table_id} does not exist")
        path = ctx.table_dir(table_id) / PARQUET_FILE
        if not path.is_file():
            raise HTTPException(409, f"table {names[table_id]} (id {table_id}) has no landed data yet")
        out[table_id] = (names[table_id], path)
    return out


def _input_evidence(ctx: AppContext, spec: CombineSpec, inputs: dict[int, tuple[str, Path]]) -> list[dict]:
    """What each input looked like when the combination was made: the same snapshot gives the same result."""
    evidence = []
    for table_id, (name, _) in inputs.items():
        snap = read_snapshot(ctx, table_id)
        alias = next((r.alias for r in (spec.base, *spec.joins) if r.table_id == table_id), "")
        evidence.append({"table_id": table_id, "table_name": name, "alias": alias,
                         "snapshot_sha256": snap.original_sha256 if snap else None, "rows": snap.rows_landed if snap else None,
                         "landed_at": snap.landed_at.isoformat(timespec="seconds") if snap else None})
    return evidence


@router.post("/preview", response_model=CombinePreview)
def preview_combination(body: CombinePreviewRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> CombinePreview:
    """Measure the combination on the real landed data without creating anything: how many records go in and come out, how many
    find no partner, whether a join would repeat records (which blocks it), and the first rows."""
    spec = CombineSpec.model_validate(body.model_dump(exclude={"sample_rows"}))
    try:
        return _combiner.preview(spec, _inputs(ctx, spec), sample_rows=body.sample_rows)
    except CombineError as exc:
        raise HTTPException(422, str(exc)) from exc


def _create(ctx: AppContext, body: CombineRequest, user: str, rebuilt_from: int | None = None) -> SourceRegistration:
    spec = CombineSpec.model_validate(body.model_dump(include=set(CombineSpec.model_fields)))
    inputs = _inputs(ctx, spec)
    scratch = _scratch(ctx)
    try:
        out_csv = scratch / "in" / f"{body.table_name}.csv"
        try:
            rows, columns, preview = _combiner.materialise(spec, inputs, out_csv)
        except CombineError as exc:
            raise HTTPException(422, str(exc)) from exc
        wanted = {k.strip().lower() for k in body.primary_key_columns if k.strip()}
        unknown = [k.strip() for k in body.primary_key_columns if k.strip() and k.strip().lower() not in {c.lower() for c in columns}]
        if unknown:
            raise HTTPException(422, f"primary_key_columns not in the combined table: {unknown}; its columns are {columns}")
        origin = {"kind": "combined", "inputs": _input_evidence(ctx, spec, inputs), "spec": spec.model_dump(), "system_name": body.system_name,
                  "table_name": body.table_name, "primary_key_columns": list(body.primary_key_columns), "rows_after_joins": preview.rows_after_joins,
                  "removed_by_filters": preview.removed_by_filters, "joins": [j.model_dump() for j in preview.joins], "warnings": preview.warnings,
                  "combined_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "combined_by": user, "rebuilt_table_id": rebuilt_from}
        options = LandingOptions(format="delimited", delimiter=",", encoding="utf-8", header=True, expected_rows=rows)
        try:
            snapshot = ctx.landing.land(out_csv, f"{body.table_name}.csv", options, scratch / "landed", user, origin=origin)
        except SourceLandingError as exc:
            raise HTTPException(422, str(exc)) from exc
        keys = {c.name.lower() for c in snapshot.columns if c.name.lower() in wanted}
        problem = key_problems(scratch / "landed" / PARQUET_FILE, [c.name for c in snapshot.columns if c.name.lower() in keys])
        if problem:
            raise HTTPException(422, problem)
        config = {"kind": "combined", "tables": [name for name, _ in inputs.values()]}
        result = _commit(ctx, scratch, body.system_name, SystemType.FLAT_FILE, config, body.table_name, body.business_name, keys, snapshot,
                         out_csv.stat().st_size)
        result.warnings = [*preview.warnings, *result.warnings]
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@router.post("", response_model=SourceRegistration, status_code=201)
def create_combination(body: CombineRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> SourceRegistration:
    """Create a new source table from the described combination. It is landed like any other source (all-text Parquet, SHA-256,
    control total = the rows the combination produced) and records which snapshot of each input table it was made from. A combination
    that would repeat records is refused unless `allow_fanout` is set; a primary key that repeats or is blank is rejected."""
    return _create(ctx, body, user)


@router.post("/{table_id}/rebuild", response_model=SourceRegistration, status_code=201)
def rebuild_combination(table_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> SourceRegistration:
    """Run a combined table's own description again on the current landed inputs (after an input was re-imported). The response
    lists which input snapshots changed since the table was last built."""
    snap = read_snapshot(ctx, table_id)
    if snap is None or snap.origin.get("kind") != "combined":
        raise HTTPException(409, f"table {table_id} was not created by combining tables")
    origin = snap.origin
    body = CombineRequest.model_validate({**origin["spec"], "system_name": origin["system_name"], "table_name": origin["table_name"],
                                          "primary_key_columns": origin.get("primary_key_columns", [])})
    inputs = _inputs(ctx, CombineSpec.model_validate(origin["spec"]))
    current = {e["table_id"]: e["snapshot_sha256"] for e in _input_evidence(ctx, CombineSpec.model_validate(origin["spec"]), inputs)}
    changed = [e["table_name"] for e in origin["inputs"] if current.get(e["table_id"]) != e["snapshot_sha256"]]
    result = _create(ctx, body, user, rebuilt_from=table_id)
    result.warnings = [(f"input snapshot changed since the last build: {', '.join(changed)}" if changed else "all inputs are the same snapshots as the last build"),
                       *result.warnings]
    return result
