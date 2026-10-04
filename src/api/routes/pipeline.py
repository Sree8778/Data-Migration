"""Run the pipeline (transform + dedupe + validate) and load (cockpit workbook or SAP)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import LoadRequest, LoadResponse, PipelineRunRequest, PipelineRunResponse
from src.db.models import CatColumn, CatTable, MapFieldRule, MapTableRule
from src.schemas.execution import DedupConfig, LineageMode, LookupFallback
from src.services.cockpit_generator import CockpitGenerator
from src.services.governance_service import GateBlockedError
from src.services.metadata_service import NotFoundError
from src.services.sap_loader_service import SapLoaderService

router = APIRouter(tags=["pipeline"])
_NAME_COLUMN = "BUT000_NAME_ORG1"
_ADDRESS_COLUMNS = ("ADRC_STREET", "ADRC_CITY1", "ADRC_POST_CODE1")


def _default_dedup(ctx: AppContext, mapping_set_id: int) -> Optional[DedupConfig]:
    """Fuzzy name (+ address) matching on whichever BP fields the mapping populates."""
    with ctx.session_factory() as s:
        staged = {
            f"{s.get(CatTable, c.table_id).table_name}_{c.column_name}"
            for c in (s.get(CatColumn, r.target_column_id)
                      for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id)))
        }
    if _NAME_COLUMN not in staged:
        return None
    return DedupConfig(name_column=_NAME_COLUMN, address_columns=[c for c in _ADDRESS_COLUMNS if c in staged])


@router.post("/pipeline/run/{mapping_set_id}", response_model=PipelineRunResponse, status_code=201)
def run_pipeline(
    mapping_set_id: int, body: PipelineRunRequest = PipelineRunRequest(),
    ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user),
) -> PipelineRunResponse:
    """Transform the uploaded extract with the mapping set, deduplicate, validate, record defects.

    Allowed for DRAFT, SUBMITTED and APPROVED mapping sets (REJECTED is refused); loading is a
    separate, gated step. Synchronous: sized for the dataset on the server's local disk.
    """
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        source_table_id = mapping.source_table_id
    source = ctx.source_file(source_table_id)
    if source is None:
        raise HTTPException(409, f"no source file uploaded for table {source_table_id}")

    dedup_config = None
    if body.deduplicate:
        dedup_config = body.dedup or _default_dedup(ctx, mapping_set_id)

    run = ctx.executor.execute_run(
        mapping_set_id, source, body.wave_name, dedup_config,
        passthrough_columns=body.passthrough_columns or None,
        lookup_fallback=LookupFallback(body.lookup_fallback), lookup_default=body.lookup_default,
        lineage_mode=LineageMode(body.lineage),
    )
    run_dir = ctx.run_dir(run.run_id)
    validation = ctx.validation.validate(
        mapping_set_id, run.deduplicated_path or run.staging_path, run_dir / "validation", run_id=run.run_id)
    (run_dir / "validation.json").write_text(validation.model_dump_json(), encoding="utf-8")
    defects = ctx.defects.record_defects(run.run_id, validation)
    return PipelineRunResponse(
        run_id=run.run_id, run=run, validation=validation, defects=defects,
        readiness=ctx.governance.evaluate_readiness(run.run_id),
        dedup_applied=run.deduplication is not None, dedup=run.deduplication)


@router.post("/pipeline/load/{run_id}", response_model=LoadResponse)
def load_run(
    run_id: int, body: LoadRequest = LoadRequest(), ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
) -> LoadResponse:
    """Generate the Migration Cockpit workbook or load into SAP (sandbox simulator or live).

    Both need (1) the steward sign-off recorded by `/governance/sign-off-load` and (2) the gate to
    pass right now: APPROVED mapping, validated successful run, zero open CRITICAL defects.
    """
    validation = ctx.load_validation_report(run_id)
    if validation is None or not validation.valid_path or not Path(validation.valid_path).is_file():
        raise HTTPException(409, f"run {run_id} has no validated payload; run the pipeline first")
    if not (ctx.run_dir(run_id) / "signoff.json").is_file():
        raise HTTPException(409, f"run {run_id} has not been signed off for load (POST /api/governance/sign-off-load/{run_id})")
    readiness = ctx.governance.evaluate_readiness(run_id)
    if not readiness.ready:
        raise GateBlockedError(readiness)

    run_dir = ctx.run_dir(run_id)
    if body.mode == "cockpit":
        cockpit = CockpitGenerator().generate(validation.valid_path, run_dir / "cockpit.xlsx")
        return LoadResponse(run_id=run_id, mode="cockpit", simulated=False, cockpit=cockpit,
                            download_url=f"/api/runs/{run_id}/cockpit")

    client, simulated = ctx.sap_client(body.target)
    result = SapLoaderService(ctx.session_factory, ctx.governance, client, batch_size=body.batch_size).load(
        run_id, validation.valid_path)
    if simulated:
        ctx.advance_sandbox(result.attempted)
    (run_dir / "load.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return LoadResponse(run_id=run_id, mode="sap", simulated=simulated, load=result)
