"""Run the pipeline (transform + dedupe + validate) and load (cockpit workbook or SAP)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from src.api.context import AppContext
from src.api.views import delta_report
from src.api.deps import get_ctx, require_connection_admin, require_user
from src.api.schemas import (DbLoadPreview, DbLoadRequest, DbLoadResult, DbRollbackRequest, DbRollbackResult, DbVerifyRequest, DbVerifyResult, LoadRequest, LoadResponse,
                             PipelineRunRequest, PipelineRunResponse)
from src.connectors.base import ConnectorError
from src.connectors.dialects import connector_for
from src.services.db_loader_service import DbLoadError
from src.db.models import CatColumn, CatTable, MapFieldRule, MapTableRule
from src.schemas.execution import DedupConfig, ExactKey, LineageMode, LookupFallback
from src.services.cockpit_generator import CockpitGenerator
from src.services.governance_service import GateBlockedError
from src.services.metadata_service import NotFoundError
from src.services.sap_loader_service import SapLoaderService

router = APIRouter(tags=["pipeline"])
_NAME_COLUMN = "BUT000_NAME_ORG1"
_ADDRESS_COLUMNS = ("ADRC_STREET", "ADRC_CITY1", "ADRC_POST_CODE1")
# generic names plus the common SAP / ERP spellings (STCD1/STCEG = tax / VAT number, SMTP_ADDR = e-mail, TELF1 = phone)
_TAX_COLUMN = re.compile(r"(^|_)(TAX|VAT|EIN|TIN|STCD\d?|STCEG|USTID|GSTIN|CNPJ|NIF)(_|$)", re.IGNORECASE)
_EMAIL_COLUMN = re.compile(r"(^|_)(E_?MAIL|SMTP|MAIL)(_|$)", re.IGNORECASE)
_PHONE_COLUMN = re.compile(r"(^|_)(PHONE|TEL|TELF\d?|TELEFON|TELEPHONE|MOBILE|HANDY)(_|$)", re.IGNORECASE)


def _columns_like(ctx: AppContext, source_table_id: int, pattern: re.Pattern) -> list[str]:
    with ctx.session_factory() as s:
        names = s.scalars(select(CatColumn.column_name).where(CatColumn.table_id == source_table_id).order_by(CatColumn.id))
        return [n for n in names if pattern.search(n)]


def _tax_columns(ctx: AppContext, source_table_id: int) -> list[str]:
    """Source columns that look like a tax / VAT number: a strong business key for exact matching."""
    return _columns_like(ctx, source_table_id, _TAX_COLUMN)


def _contact_keys(ctx: AppContext, source_table_id: int) -> tuple[list[str], list[str]]:
    """Source columns that look like an e-mail address / a phone number (default exact keys, normalised)."""
    return _columns_like(ctx, source_table_id, _EMAIL_COLUMN), _columns_like(ctx, source_table_id, _PHONE_COLUMN)


def _default_dedup(
    ctx: AppContext, mapping_set_id: int, tax_columns: list[str], email_columns: list[str] = (), phone_columns: list[str] = (),
) -> Optional[DedupConfig]:
    """Exact match on tax / VAT numbers, e-mail addresses and phone numbers (whichever the source has) plus fuzzy
    name (+ address) matching on whichever BP fields the mapping populates. Send an explicit `dedup` config to
    change this, e.g. to drop the phone key when branches legitimately share a switchboard number."""
    with ctx.session_factory() as s:
        staged = {
            f"{s.get(CatTable, c.table_id).table_name}_{c.column_name}"
            for c in (s.get(CatColumn, r.target_column_id)
                      for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id)))
        }
    keys = [ExactKey(name=f"tax_{c.lower()}", columns=[f"SRC_{c}"], normalizer="alnum") for c in tax_columns]
    keys += [ExactKey(name=f"email_{c.lower()}", columns=[f"SRC_{c}"], normalizer="email") for c in email_columns]
    keys += [ExactKey(name=f"phone_{c.lower()}", columns=[f"SRC_{c}"], normalizer="digits") for c in phone_columns]
    if _NAME_COLUMN not in staged and not keys:
        return None
    return DedupConfig(
        exact_keys=keys, name_column=_NAME_COLUMN if _NAME_COLUMN in staged else None,
        address_columns=[c for c in _ADDRESS_COLUMNS if c in staged] if _NAME_COLUMN in staged else [])


def _legacy_columns(ctx: AppContext, mapping_set_id: int) -> list[str]:
    """Keep the raw legacy value of every mapped source column (as SRC_<column>) so stewards can see
    what the legacy system said when a value has to be constructed or a lookup failed."""
    with ctx.session_factory() as s:
        names = [s.get(CatColumn, r.source_column_id).column_name for r in s.scalars(
            select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id, MapFieldRule.source_column_id.is_not(None)))]
    return list(dict.fromkeys(names))


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
    archived = ctx.archive.get(mapping_set_id)
    if archived:
        raise HTTPException(409, f"mapping set {mapping_set_id} is archived ({archived['reason']}); restore it or use the current mapping set")
    source = ctx.source_file(source_table_id)
    if source is None:
        raise HTTPException(409, f"no source file uploaded for table {source_table_id}")

    in_file = ctx.file_columns(source_table_id) or set()  # the catalog can still hold columns of an earlier layout
    tax_columns = [c for c in _tax_columns(ctx, source_table_id) if c.lower() in in_file]
    email_columns, phone_columns = (
        [c for c in cols if c.lower() in in_file] for cols in _contact_keys(ctx, source_table_id))
    key_columns = tax_columns + email_columns + phone_columns
    dedup_config = None
    if body.deduplicate:
        dedup_config = body.dedup or _default_dedup(ctx, mapping_set_id, tax_columns, email_columns, phone_columns)

    run = ctx.executor.execute_run(
        mapping_set_id, source, body.wave_name, dedup_config, exclusions=ctx.exclusions.rules(mapping_set_id),
        passthrough_columns=list(dict.fromkeys(
            body.passthrough_columns + _legacy_columns(ctx, mapping_set_id) + (key_columns if body.deduplicate and not body.dedup else []))) or None,
        lookup_fallback=LookupFallback(body.lookup_fallback), lookup_default=body.lookup_default,
        lineage_mode=LineageMode(body.lineage),
    )
    run_dir = ctx.run_dir(run.run_id)
    snapshot_file = ctx.table_dir(source_table_id) / "snapshot.json"
    if snapshot_file.is_file():  # evidence of exactly which landed file this run read
        shutil.copyfile(snapshot_file, run_dir / "source_snapshot.json")
    if dedup_config is not None:  # remembered so Data Construction can repeat deduplication after merging values
        (run_dir / "dedup_config.json").write_text(dedup_config.model_dump_json(), encoding="utf-8")
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


def _target_connector(ctx: AppContext, name: str, schema: Optional[str]):
    record = ctx.connections.get(name)
    if record is None:
        raise HTTPException(404, f"connection '{name}' does not exist")
    if record.role != "target":
        raise HTTPException(409, f"connection '{name}' is a source connection (read-only); choose a target connection")
    schema = schema or record.default_schema
    if not schema:
        raise HTTPException(422, "no schema given and the target connection has no default schema")
    try:
        return connector_for(ctx.connections.spec_for(name)), schema
    except ConnectorError as exc:
        raise HTTPException(409 if "password" in str(exc) else 422, str(exc)) from exc


def _validated_payload(ctx: AppContext, run_id: int) -> str:
    validation = ctx.load_validation_report(run_id)
    if validation is None or not validation.valid_path or not Path(validation.valid_path).is_file():
        raise HTTPException(409, f"run {run_id} has no validated payload; run the pipeline first")
    return validation.valid_path


def _deliver_delta(ctx: AppContext, connector, run_id: int, valid_path: str, body: DbLoadRequest, schema: str, user: str) -> dict:
    """mode=merge: apply this run's delta to the table that the baseline run delivered."""
    if not body.baseline_run:
        raise HTTPException(422, "merge needs baseline_run: the earlier run whose delivery this run updates")
    table = connector.natural_case(body.table_name)
    earlier = [d for d in ctx.db_loader.deliveries(ctx.run_dir(body.baseline_run))
               if (d["connection"], d["schema"], d["table"]) == (body.connection, schema, table)]
    if not earlier or earlier[-1].get("rolled_back"):
        raise HTTPException(409, f"run {body.baseline_run} has no live delivery into {schema}.{table} on connection '{body.connection}', so there is nothing to merge into; "
                                 "choose the run that delivered that table")
    if bool(earlier[-1].get("include_source_columns")) != body.include_source_columns:
        raise HTTPException(422, f"run {body.baseline_run} delivered {'with' if earlier[-1].get('include_source_columns') else 'without'} the raw legacy columns; "
                                 "a merge must use the same setting")
    report = delta_report(ctx, run_id, body.baseline_run)
    return ctx.db_loader.deliver_merge(
        connector, run_id, valid_path, report.delta_path, report.removed_path, body.baseline_run, ctx.run_dir(run_id), body.connection, schema, body.table_name, user,
        include_source_columns=body.include_source_columns, delete_removed=body.delete_removed, batch_size=min(body.batch_size, 500), verify_values=body.verify_values)


@router.get("/pipeline/load-db/{run_id}/preview", response_model=DbLoadPreview)
def preview_db_load(run_id: int, connection: str, table_name: str, schema_name: Optional[str] = None, include_source_columns: bool = False,
                    ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> DbLoadPreview:
    """What a database delivery would create: the table, its columns and the CREATE TABLE statement. Nothing is executed."""
    connector, schema = _target_connector(ctx, connection, schema_name)
    try:
        plan = ctx.db_loader.preview(connector, _validated_payload(ctx, run_id), run_id, schema, table_name, include_source_columns)
    except (ConnectorError, DbLoadError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return DbLoadPreview(run_id=run_id, connection=connection, schema_name=schema, **plan)


@router.post("/pipeline/load-db/{run_id}", response_model=DbLoadResult, status_code=201)
def load_run_into_database(run_id: int, body: DbLoadRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> DbLoadResult:
    """Deliver the validated records of a run into a database target (a new table, created and filled all-or-nothing).

    Same gate as every load: the steward sign-off must be recorded and the governance gate must pass right now. The row count read
    back from the new table must equal the validated record count, or nothing is kept. The delivery is recorded in the run folder
    and checked by the reconciliation certificate. Allowed once per table name unless `mode` is `replace`."""
    valid_path = _validated_payload(ctx, run_id)
    if not (ctx.run_dir(run_id) / "signoff.json").is_file():
        raise HTTPException(409, f"run {run_id} has not been signed off for load (POST /api/governance/sign-off-load/{run_id})")
    readiness = ctx.governance.evaluate_readiness(run_id)
    if not readiness.ready:
        raise GateBlockedError(readiness)
    connector, schema = _target_connector(ctx, body.connection, body.schema_name)
    try:
        if body.mode == "merge":
            record = _deliver_delta(ctx, connector, run_id, valid_path, body, schema, user)
        else:
            record = ctx.db_loader.deliver(connector, run_id, valid_path, ctx.run_dir(run_id), body.connection, schema, body.table_name, body.mode, user,
                                           include_source_columns=body.include_source_columns, batch_size=body.batch_size, verify_values=body.verify_values)
    except DbLoadError as exc:
        raise HTTPException(422, str(exc)) from exc
    return DbLoadResult(schema_name=schema, **{k: v for k, v in record.items() if k != "schema"})


@router.post("/pipeline/verify-db/{run_id}", response_model=DbVerifyResult)
def verify_db_delivery(run_id: int, body: DbVerifyRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> DbVerifyResult:
    """Compare what a delivered table holds NOW with the run's validated payload, value by value (a content fingerprint per column).
    Catches a table that was edited, truncated or partly deleted after the delivery, and names the columns that differ. Read-only on
    the target; the result is stored with the delivery and shown on the reconciliation certificate."""
    valid_path = _validated_payload(ctx, run_id)
    connector, schema = _target_connector(ctx, body.connection, body.schema_name)
    try:
        result = ctx.db_loader.verify(connector, run_id, valid_path, ctx.run_dir(run_id), body.connection, schema, body.table_name, user)
    except (ConnectorError, DbLoadError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return DbVerifyResult(schema_name=result.pop("schema"), **result)


@router.post("/pipeline/rollback-db/{run_id}", response_model=DbRollbackResult)
def rollback_db_delivery(run_id: int, body: DbRollbackRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> DbRollbackResult:
    """Undo a delivery into a database target: exactly the rows of this run are deleted (one transaction, counts checked before it is
    committed) and the table is dropped when nothing else is left in it. Needs a reason. Refused when the table no longer holds the
    rows that were delivered (someone changed it since). The rollback is recorded and shown on the reconciliation certificate. This
    undoes DATABASE deliveries only: a load into SAP is reversed in SAP."""
    connector, schema = _target_connector(ctx, body.connection, body.schema_name)
    try:
        result = ctx.db_loader.rollback(connector, run_id, ctx.run_dir(run_id), body.connection, schema, body.table_name, user, body.reason)
    except DbLoadError as exc:
        raise HTTPException(422, str(exc)) from exc
    return DbRollbackResult(schema_name=result.pop("schema"), **result)


@router.get("/pipeline/load-db/{run_id}", response_model=list[DbLoadResult])
def list_db_loads(run_id: int, ctx: AppContext = Depends(get_ctx)) -> list[DbLoadResult]:
    """Deliveries of this run into database targets."""
    return [DbLoadResult(schema_name=r["schema"], **{k: v for k, v in r.items() if k != "schema"}) for r in ctx.db_loader.deliveries(ctx.run_dir(run_id))]
