"""Source registration / upload, table listing, profiling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select

from src.api.context import SOURCE_SUFFIXES, AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import ProfileResponse, SourceRegistration, TableSummary
from src.api.views import column_health
from src.db.enums import PiiClassification, SystemType, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable
from src.schemas.metadata import BulkColumnRegistration, ColumnCreate, DataSourceCreate, TableCreate
from src.services._duckdb_util import file_relation
from src.services.metadata_service import NotFoundError

router = APIRouter(tags=["sources"])
_CHUNK = 1 << 20
_PII = (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII)


@router.post("/sources/register", response_model=SourceRegistration, status_code=201)
def register_source(
    system_name: str = Form(..., min_length=1, max_length=128),
    system_type: SystemType = Form(SystemType.FLAT_FILE),
    table_name: Optional[str] = Form(None, max_length=128),
    business_name: Optional[str] = Form(None, max_length=256),
    primary_key_columns: Optional[str] = Form(None, description="comma separated"),
    connection_config: Optional[str] = Form(None, description="JSON object; use *_ref for secrets"),
    file: Optional[UploadFile] = File(None),
    ctx: AppContext = Depends(get_ctx),
    user: str = Depends(require_user),
) -> SourceRegistration:
    """Register a legacy system and, when a CSV/Parquet extract is uploaded, its table and columns.

    Re-uploading for the same table name replaces the file and refreshes the column list.
    """
    if file is not None and not table_name:
        raise HTTPException(422, "table_name is required when a file is uploaded")
    try:
        config = json.loads(connection_config) if connection_config else {}
        if not isinstance(config, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(422, "connection_config must be a JSON object") from None

    source = ctx.metadata.register_data_source(DataSourceCreate(
        system_name=system_name, system_type=system_type, target_type=TargetType.SOURCE, connection_config=config))
    if not table_name:
        return SourceRegistration(source_id=source.id, system_name=source.system_name)

    table = ctx.metadata.register_table(TableCreate(source_id=source.id, table_name=table_name, business_name=business_name))
    stored = 0
    filename = None
    columns_registered = 0
    if file is not None:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in SOURCE_SUFFIXES:
            raise HTTPException(422, f"unsupported file type '{suffix}'; upload .csv or .parquet")
        directory = ctx.table_dir(table.id)
        directory.mkdir(parents=True, exist_ok=True)
        for old in SOURCE_SUFFIXES:  # a table has exactly one current source file
            (directory / f"source{old}").unlink(missing_ok=True)
        target = directory / f"source{suffix}"
        stored = _store_upload(file, target, ctx.settings.max_upload_mb)
        filename = file.filename
        keys = {k.strip().lower() for k in (primary_key_columns or "").split(",") if k.strip()}
        columns = _describe(target)
        unknown = keys - {c.lower() for c, _ in columns}
        if unknown:
            target.unlink(missing_ok=True)
            raise HTTPException(422, f"primary_key_columns not in file: {sorted(unknown)}")
        registered = ctx.metadata.bulk_register_columns(BulkColumnRegistration(table_id=table.id, columns=[
            ColumnCreate(column_name=name, data_type=dtype, is_primary_key=name.lower() in keys)
            for name, dtype in columns]))
        columns_registered = len(registered)
        (directory / "profile.json").unlink(missing_ok=True)  # stale after a new upload
    return SourceRegistration(
        source_id=source.id, system_name=source.system_name, table_id=table.id, table_name=table.table_name,
        columns_registered=columns_registered, filename=filename, bytes_stored=stored)


def _store_upload(upload: UploadFile, target: Path, limit_mb: int) -> int:
    """Stream to disk with a hard size limit (written to a temp name first, then moved in place)."""
    limit = limit_mb * 1024 * 1024
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


def _describe(path: Path) -> list[tuple[str, str]]:
    typed = file_relation(path, all_varchar=False)[1]
    try:
        rows = duckdb.connect().execute(f"DESCRIBE SELECT * FROM {typed}").fetchall()
    except duckdb.Error as exc:
        raise HTTPException(422, f"cannot read the uploaded file: {str(exc).splitlines()[0][:200]}") from exc
    if not rows:
        raise HTTPException(422, "the file has no columns")
    return [(r[0], r[1]) for r in rows]


@router.get("/tables", response_model=list[TableSummary])
def list_tables(ctx: AppContext = Depends(get_ctx)) -> list[TableSummary]:
    """Registered legacy (non-target) tables."""
    with ctx.session_factory() as s:
        rows = s.execute(
            select(CatTable, CatDataSource, select(func.count()).select_from(CatColumn)
                   .where(CatColumn.table_id == CatTable.id).scalar_subquery())
            .join(CatDataSource, CatTable.source_id == CatDataSource.id)
            .where(CatDataSource.target_type != TargetType.TARGET).order_by(CatTable.id)).all()
        return [TableSummary(
            table_id=t.id, table_name=t.table_name, system_name=src.system_name, system_type=src.system_type.value,
            row_count_estimate=t.row_count_estimate, last_profiled_at=t.last_profiled_at,
            has_source_file=ctx.source_file(t.id) is not None, column_count=int(n)) for t, src, n in rows]


@router.post("/profile/{table_id}", response_model=ProfileResponse)
def run_profile(table_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> ProfileResponse:
    """Profile the uploaded extract with DuckDB, persist the stats to the catalog, return health scores."""
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

    result = ctx.profiler.profile_file(source)
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
