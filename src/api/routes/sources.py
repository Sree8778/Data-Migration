"""Source registration / upload (multi-format), preview, snapshots, table listing, profiling."""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy import func, select

from src.api.context import SOURCE_SUFFIXES, AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import DbImportRequest, FileImportRequest, ProfileResponse, SourceRegistration, TableSummary
from src.connectors.base import ConnectorError
from src.connectors.dialects import CONNECTORS, connector_for
from src.connectors.registry import family
from src.connectors.storage import storage_for
from src.api.views import column_health
from src.db.enums import PiiClassification, SystemType, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule
from src.schemas.metadata import BulkColumnRegistration, ColumnCreate, DataSourceCreate, TableCreate
from src.schemas.source import LandingOptions, SnapshotInfo, SourcePreview
from src.services.metadata_service import NotFoundError
from src.services.source_landing import PARQUET_FILE, SNAPSHOT_FILE, SourceLandingError, key_problems

router = APIRouter(tags=["sources"])
_CHUNK = 1 << 20
_PII = (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII)


def _parse_options(raw: Optional[str], expected_rows: Optional[int] = None) -> LandingOptions:
    try:
        options = LandingOptions.model_validate_json(raw) if raw else LandingOptions()
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise HTTPException(422, f"invalid options: {problems}") from None
    return options.model_copy(update={"expected_rows": expected_rows}) if expected_rows is not None else options


_SNAPSHOT_CACHE: dict[str, tuple[int, int, Optional[SnapshotInfo]]] = {}   # path -> (mtime_ns, size, parsed): the table listing is polled by the UI


def read_snapshot(ctx: AppContext, table_id: int) -> Optional[SnapshotInfo]:
    """The landed snapshot's record, or None when there is none or it cannot be read: one damaged file must not break every listing."""
    path = ctx.table_dir(table_id) / SNAPSHOT_FILE
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _SNAPSHOT_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        parsed: Optional[SnapshotInfo] = SnapshotInfo.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = None
    _SNAPSHOT_CACHE[key] = (stat.st_mtime_ns, stat.st_size, parsed)
    return parsed


def _store_upload(upload: UploadFile, target: Path, limit_mb: int) -> int:
    """Stream to disk with a hard size limit (written to a temp name first, then moved in place)."""
    limit = limit_mb * 1024 * 1024
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    written = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := upload.file.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"file larger than {limit_mb} MB")
                out.write(chunk)
        if written == 0:
            raise HTTPException(422, "uploaded file is empty")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return written


def _scratch(ctx: AppContext) -> Path:
    path = ctx.workspace / "tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload_name(upload: UploadFile) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]", "_", Path(upload.filename or "upload").name) or "upload"


@router.post("/sources/preview", response_model=SourcePreview)
def preview_source(
    file: UploadFile = File(...),
    options: Optional[str] = Form(None, description="LandingOptions as JSON; omitted values are detected"),
    ctx: AppContext = Depends(get_ctx),
    user: str = Depends(require_user),
) -> SourcePreview:
    """Show how a file would be read (format, encoding, delimiter, sheets, first rows) before registering it.

    Nothing is stored. Re-send with corrected `options` until the preview looks right."""
    scratch = _scratch(ctx)
    try:
        name = _upload_name(file)
        _store_upload(file, scratch / name, ctx.settings.max_upload_mb)
        return ctx.landing.preview(scratch / name, name, _parse_options(options))
    except SourceLandingError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _retire_stale_columns(ctx: AppContext, table_id: int, current: set[str]) -> list[str]:
    """A re-upload with a different layout must not leave the old layout's columns in the catalog: they get picked up
    as key / pass-through columns and shown as mappable although the file no longer has them. Columns still used by a
    mapping rule are kept (deleting them would orphan the rule) and reported so the steward can remap."""
    with ctx.session_factory.begin() as s:
        stale = [c for c in s.scalars(select(CatColumn).where(CatColumn.table_id == table_id)) if c.column_name.lower() not in current]
        used = set(s.scalars(select(MapFieldRule.source_column_id).where(MapFieldRule.source_column_id.in_([c.id for c in stale])))) if stale else set()
        for c in stale:
            if c.id not in used:
                s.delete(c)
        kept = sorted(c.column_name for c in stale if c.id in used)
    return [f"mapping rules still reference column(s) no longer in this file: {', '.join(kept)}; remap them before running the pipeline"] if kept else []


def _commit(ctx: AppContext, scratch: Path, system_name: str, system_type: SystemType, config: dict, table_name: Optional[str],
            business_name: Optional[str], keys: set[str], snapshot: Optional[SnapshotInfo], stored: int) -> SourceRegistration:
    """Write a landed snapshot into the catalog and move its files into the table's folder. Shared by file uploads and database imports,
    so both leave exactly the same evidence (parquet, snapshot.json, original)."""
    source = ctx.metadata.register_data_source(DataSourceCreate(
        system_name=system_name, system_type=system_type, target_type=TargetType.SOURCE, connection_config=config))
    if not table_name:
        return SourceRegistration(source_id=source.id, system_name=source.system_name)
    table = ctx.metadata.register_table(TableCreate(source_id=source.id, table_name=table_name, business_name=business_name))
    if snapshot is None:
        return SourceRegistration(source_id=source.id, system_name=source.system_name, table_id=table.id, table_name=table.table_name)

    registered = ctx.metadata.bulk_register_columns(BulkColumnRegistration(table_id=table.id, columns=[
        ColumnCreate(column_name=c.name, data_type=c.detected_type, is_primary_key=c.name.lower() in keys)
        for c in snapshot.columns]))
    warnings = list(snapshot.warnings)
    warnings += _retire_stale_columns(ctx, table.id, {c.name.lower() for c in snapshot.columns})
    directory = ctx.table_dir(table.id)
    directory.mkdir(parents=True, exist_ok=True)
    for old in [*(directory / f"source{s}" for s in SOURCE_SUFFIXES), directory / SNAPSHOT_FILE, directory / "profile.json"]:
        old.unlink(missing_ok=True)  # a table has exactly one current source snapshot
    shutil.rmtree(directory / "original", ignore_errors=True)
    shutil.move(str(scratch / "landed" / PARQUET_FILE), str(directory / PARQUET_FILE))
    shutil.move(str(scratch / "landed" / SNAPSHOT_FILE), str(directory / SNAPSHOT_FILE))
    shutil.move(str(scratch / "landed" / "original"), str(directory / "original"))
    return SourceRegistration(
        source_id=source.id, system_name=source.system_name, table_id=table.id, table_name=table.table_name,
        columns_registered=len(registered), filename=snapshot.original_filename, bytes_stored=stored,
        format=snapshot.format, rows_landed=snapshot.rows_landed, control_status=snapshot.control_status,
        warnings=warnings, origin=snapshot.origin or None)


@router.post("/sources/register", response_model=SourceRegistration, status_code=201)
def register_source(
    system_name: str = Form(..., min_length=1, max_length=128),
    system_type: SystemType = Form(SystemType.FLAT_FILE),
    table_name: Optional[str] = Form(None, max_length=128),
    business_name: Optional[str] = Form(None, max_length=256),
    primary_key_columns: Optional[str] = Form(None, description="comma separated"),
    connection_config: Optional[str] = Form(None, description="JSON object; use *_ref for secrets"),
    options: Optional[str] = Form(None, description="LandingOptions as JSON (format, encoding, delimiter, sheet...)"),
    expected_rows: Optional[int] = Form(None, ge=0, description="control total: row count reported by the source system"),
    file: Optional[UploadFile] = File(None),
    ctx: AppContext = Depends(get_ctx),
    user: str = Depends(require_user),
) -> SourceRegistration:
    """Register a legacy system and, when a file is uploaded, land it as a snapshot and register its table.

    Formats: delimited text, Excel (.xlsx), JSON / NDJSON, fixed-width (with a column layout), Parquet,
    optionally gzip or zip. The file is landed and checked BEFORE anything is written to the catalog, so a
    rejected file leaves nothing behind. Re-uploading the same table name replaces its snapshot."""
    if file is not None and not table_name:
        raise HTTPException(422, "table_name is required when a file is uploaded")
    try:
        config = json.loads(connection_config) if connection_config else {}
        if not isinstance(config, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(422, "connection_config must be a JSON object") from None
    landing_options = _parse_options(options, expected_rows)

    scratch = _scratch(ctx)
    try:
        snapshot: Optional[SnapshotInfo] = None
        stored = 0
        if file is not None:
            name = _upload_name(file)
            stored = _store_upload(file, scratch / "in" / name, ctx.settings.max_upload_mb)
            try:
                snapshot = ctx.landing.land(scratch / "in" / name, name, landing_options, scratch / "landed", user)
            except SourceLandingError as exc:
                raise HTTPException(422, str(exc)) from exc
            keys = {k.strip().lower() for k in (primary_key_columns or "").split(",") if k.strip()}
            unknown = keys - {c.name.lower() for c in snapshot.columns}
            if unknown:
                raise HTTPException(422, f"primary_key_columns not in file: {sorted(unknown)}")
            problem = key_problems(scratch / "landed" / PARQUET_FILE, [c.name for c in snapshot.columns if c.name.lower() in keys])
            if problem:
                raise HTTPException(422, problem)
        else:
            keys = set()

        return _commit(ctx, scratch, system_name, system_type, config, table_name, business_name, keys, snapshot, stored)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@router.post("/sources/import-db", response_model=SourceRegistration, status_code=201)
def import_from_database(body: DbImportRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> SourceRegistration:
    """Extract one table (or view) from a saved database connection, land it as a snapshot and register it.

    Read-only on the source. The row count is taken in the same transaction as the extract and, unless `control_total` is
    switched off, the landing is rejected if the rows read differ from it. The primary key defaults to the one defined in the
    database, and a key that repeats or is blank is rejected like it is for files. Re-importing the same table name replaces
    its snapshot. Runs synchronously: sized for tables that extract within a few minutes."""
    started = time.monotonic()
    try:
        spec = ctx.connections.spec_for(body.connection)
    except ConnectorError as exc:
        raise HTTPException(409 if "password" in str(exc) else 404, str(exc)) from exc
    record = ctx.connections.get(body.connection)
    if record is not None and record.role != "source":
        raise HTTPException(409, f"connection '{body.connection}' is a target connection; data is imported from source connections")
    if family(spec.dialect) != "database":
        raise HTTPException(409, f"connection '{body.connection}' is file storage; import its files with /api/sources/import-file")
    connector = connector_for(spec)
    scratch = _scratch(ctx)
    try:
        try:
            columns_meta = connector.columns(body.schema_name, body.table)
            extract = connector.extract_csv(body.schema_name, body.table, body.columns, scratch / "in" / f"{body.table}.csv.gz",
                                            ctx.settings.max_extract_mb * 1024 * 1024)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        if extract.rows == 0:
            raise HTTPException(422, f"{body.schema_name}.{body.table} has no rows to import")
        origin = {"kind": "database", "connection": body.connection, "dialect": spec.dialect, "host": spec.host, "port": spec.port, "database": spec.database,
                  "schema": body.schema_name, "table": body.table, "user": spec.user, "source_row_count": extract.source_count,
                  "extract_seconds": round(extract.seconds, 2), "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        options = LandingOptions(format="delimited", delimiter=",", encoding="utf-8", header=True,
                                 expected_rows=extract.source_count if body.control_total else None)
        try:
            snapshot = ctx.landing.land(extract.path, f"{body.schema_name}.{body.table}.csv.gz", options, scratch / "landed", user, origin=origin)
        except SourceLandingError as exc:
            raise HTTPException(422, str(exc)) from exc

        defined = [c.name for c in columns_meta if c.is_primary_key and (body.columns is None or c.name in body.columns)]
        wanted = body.primary_key_columns if body.primary_key_columns is not None else defined
        keys = {k.lower() for k in wanted}
        unknown = keys - {c.name.lower() for c in snapshot.columns}
        if unknown:
            raise HTTPException(422, f"primary_key_columns not in the extract: {sorted(unknown)}")
        problem = key_problems(scratch / "landed" / PARQUET_FILE, [c.name for c in snapshot.columns if c.name.lower() in keys])
        if problem:
            raise HTTPException(422, problem)
        config = {"connection": body.connection, "dialect": spec.dialect, "host": spec.host, "port": spec.port, "database": spec.database, "user": spec.user}
        result = _commit(ctx, scratch, body.system_name or body.connection, SystemType(CONNECTORS[spec.dialect].system_type), config,
                         body.table_name or body.table, body.business_name, keys, snapshot, extract.bytes_written)
        result.extract_seconds = round(time.monotonic() - started, 2)
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _table_from_key(key: str) -> str:
    """AR_Customers_2024.csv.gz -> AR_CUSTOMERS_2024"""
    base = re.sub(r"\.(gz|zip)$", "", Path(key).name, flags=re.IGNORECASE)
    base = re.sub(r"\.[^.]+$", "", base)
    name = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").upper() or "SOURCE_FILE"
    return name if not name[0].isdigit() else "T_" + name


@router.post("/sources/import-file", response_model=SourceRegistration, status_code=201)
def import_remote_file(body: FileImportRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> SourceRegistration:
    """Download one file from a saved storage connection (Azure Blob / Data Lake, S3, SFTP), land it like an uploaded file and register it.

    Read-only on the remote side. The same landing as an upload: format, encoding and delimiter detection, control total (when given),
    snapshot, lineage; a declared key that repeats or is blank is rejected. The file is capped at MIGRATION_MAX_UPLOAD_MB."""
    record = ctx.connections.get(body.connection)
    if record is None:
        raise HTTPException(404, f"connection '{body.connection}' does not exist")
    if record.role != "source" or family(record.dialect) != "storage":
        raise HTTPException(409, f"connection '{body.connection}' is not a file-storage source connection")
    try:
        spec = ctx.connections.spec_for(body.connection)
    except ConnectorError as exc:
        raise HTTPException(409, str(exc)) from exc
    connector = storage_for(spec)
    landing_options = _parse_options(json.dumps(body.options) if body.options else None, body.expected_rows)
    scratch = _scratch(ctx)
    try:
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(body.key).name) or "download"
        try:
            size = connector.download(body.container, body.key, scratch / "in" / name, ctx.settings.max_upload_mb * 1024 * 1024)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        if size == 0:
            raise HTTPException(422, f"{body.key} is empty")
        origin = {"kind": "storage", "connection": body.connection, "provider": spec.dialect, "host": spec.host, "container": body.container,
                  "key": body.key, "bytes": size, "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            snapshot = ctx.landing.land(scratch / "in" / name, name, landing_options, scratch / "landed", user, origin=origin)
        except SourceLandingError as exc:
            raise HTTPException(422, str(exc)) from exc
        keys = {k.strip().lower() for k in (body.primary_key_columns or []) if k.strip()}
        unknown = keys - {c.name.lower() for c in snapshot.columns}
        if unknown:
            raise HTTPException(422, f"primary_key_columns not in the file: {sorted(unknown)}")
        problem = key_problems(scratch / "landed" / PARQUET_FILE, [c.name for c in snapshot.columns if c.name.lower() in keys])
        if problem:
            raise HTTPException(422, problem)
        config = {"connection": body.connection, "provider": spec.dialect, "host": spec.host, "container": body.container}
        return _commit(ctx, scratch, body.system_name or body.connection, SystemType.FLAT_FILE, config, body.table_name or _table_from_key(body.key),
                       body.business_name, keys, snapshot, size)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@router.get("/tables", response_model=list[TableSummary])
def list_tables(ctx: AppContext = Depends(get_ctx)) -> list[TableSummary]:
    """Registered legacy (non-target) tables."""
    with ctx.session_factory() as s:
        rows = s.execute(
            select(CatTable, CatDataSource, select(func.count()).select_from(CatColumn)
                   .where(CatColumn.table_id == CatTable.id).scalar_subquery())
            .join(CatDataSource, CatTable.source_id == CatDataSource.id)
            .where(CatDataSource.target_type != TargetType.TARGET).order_by(CatTable.id)).all()
        out = []
        for t, src, n in rows:
            snap = read_snapshot(ctx, t.id)
            out.append(TableSummary(
                table_id=t.id, table_name=t.table_name, system_name=src.system_name, system_type=src.system_type.value,
                row_count_estimate=t.row_count_estimate, last_profiled_at=t.last_profiled_at,
                has_source_file=ctx.source_file(t.id) is not None, column_count=int(n),
                source_format=snap.format if snap else None, rows_landed=snap.rows_landed if snap else None))
        return out


@router.get("/tables/{table_id}/snapshot", response_model=SnapshotInfo)
def get_snapshot(table_id: int, ctx: AppContext = Depends(get_ctx)) -> SnapshotInfo:
    """What was landed for a table: format, detection results, control totals, checksum, warnings."""
    snapshot = read_snapshot(ctx, table_id)
    if snapshot is None:
        raise HTTPException(404, f"table {table_id} has no landed snapshot")
    return snapshot


@router.post("/profile/{table_id}", response_model=ProfileResponse)
def run_profile(table_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> ProfileResponse:
    """Profile the landed extract with DuckDB, persist the stats to the catalog, return health scores."""
    with ctx.session_factory() as s:
        table = s.get(CatTable, table_id)
        if table is None:
            raise NotFoundError(f"table {table_id} does not exist")
        table_name = table.table_name
        pii = {c.column_name.lower() for c in s.scalars(select(CatColumn).where(
            CatColumn.table_id == table_id, CatColumn.pii_classification.in_(_PII)))}
    source = ctx.source_file(table_id)
    if source is None:
        raise HTTPException(409, f"no source file uploaded for table {table_id}; register it with a file first")

    snapshot = read_snapshot(ctx, table_id)
    result = ctx.profiler.profile_file(source, detected_types=snapshot.detected_types if snapshot else None)
    ctx.profiler.save_profile_to_catalog(table_id, result)
    columns = [column_health(c, c.column_name.lower() in pii) for c in result.columns]
    inferred = [k.columns for k in result.key_duplicates]
    response = ProfileResponse(
        table_id=table_id, table_name=table_name, profiled_at=result.profiled_at, total_rows=result.total_rows,
        column_count=result.column_count,
        average_health_score=round(sum(c.health_score for c in columns) / len(columns), 1) if columns else 0.0,
        full_row_duplicate_rows=result.full_row_duplicate_rows, full_row_duplicate_ratio=result.full_row_duplicate_ratio,
        key_duplicates=result.key_duplicates, inferred_keys=inferred, columns=columns)
    (ctx.table_dir(table_id) / "profile.json").write_text(response.model_dump_json(), encoding="utf-8")  # PII-masked copy
    return response


@router.get("/profile/{table_id}", response_model=ProfileResponse)
def get_profile(table_id: int, ctx: AppContext = Depends(get_ctx)) -> ProfileResponse:
    path = ctx.table_dir(table_id) / "profile.json"
    if not path.is_file():
        raise HTTPException(404, f"table {table_id} has not been profiled yet")
    return ProfileResponse.model_validate_json(path.read_text(encoding="utf-8"))
