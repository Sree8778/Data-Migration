"""Database connections: save, test and browse legacy databases (read-only). Passwords are never returned or stored."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from src.api.context import AppContext
from src.api.deps import get_ctx, require_connection_admin, require_user
from src.api.schemas import (
    ConnectionIn,
    ConnectionOut,
    ConnectionTestIn,
    ConnectionTestResult,
    WriteTestIn,
    DbColumn,
    DbPreview,
    DbTable,
    DialectInfo,
    StorageFile,
    PresetInfo,
)
from src.connectors.base import Connector, ConnectorAuthError, ConnectorError
from src.connectors.dialects import CONNECTORS, connector_for
from src.connectors.registry import ALL_KINDS, DATABASE_LABEL, family, info
from src.connectors.storage import HostKeyError, StorageConnector, storage_for
from src.connectors.store import ConnectionInputError, ConnectionRecord

router = APIRouter(prefix="/connections", tags=["connections"])


def _out(ctx: AppContext, r: ConnectionRecord) -> ConnectionOut:
    return ConnectionOut(
        name=r.name, dialect=r.dialect, label=info(r.dialect).label, host=r.host, port=r.port, database=r.database, user=r.user,
        password_source=ctx.connections.password_source(r), options=r.options, role=r.role, default_schema=r.default_schema,
        created_by=r.created_by, updated_at=r.updated_at)


def _storage(ctx: AppContext, name: str) -> StorageConnector:
    try:
        spec = ctx.connections.spec_for(name)
    except ConnectorError as exc:
        raise HTTPException(409 if "password" in str(exc) else 404 if "does not exist" in str(exc) else 422, str(exc)) from exc
    if family(spec.dialect) != "storage":
        raise HTTPException(409, f"connection '{name}' is a database; use its schemas and tables instead")
    return storage_for(spec)


def _connector(ctx: AppContext, name: str) -> Connector:
    record = ctx.connections.get(name)
    if record is not None and family(record.dialect) != "database":
        raise HTTPException(409, f"connection '{name}' is file storage; use its containers and files instead")
    try:
        return connector_for(ctx.connections.spec_for(name))
    except ConnectorError as exc:
        raise HTTPException(409 if "password" in str(exc) else 404 if "does not exist" in str(exc) else 422, str(exc)) from exc


def _guard(call):
    """Run a connector call and turn driver / network trouble into a clear HTTP error."""
    try:
        return call()
    except ConnectorAuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/dialects", response_model=list[DialectInfo])
def dialects() -> list[DialectInfo]:
    return [DialectInfo(dialect=k, label=info(k).label, default_port=info(k).default_port, database_label=DATABASE_LABEL[k], family=family(k) or "database") for k in ALL_KINDS]


PRESETS = [
    PresetInfo(id="azure-sql", label="Azure SQL Database / Managed Instance", dialect="mssql", port=1433, options={"encrypt": True, "validate_host": True},
               hint="Server: <name>.database.windows.net. Encryption is always on. Use a SQL login; Azure Active Directory sign-in is not supported yet."),
    PresetInfo(id="azure-postgresql", label="Azure Database for PostgreSQL", dialect="postgresql", port=5432, options={"sslmode": "require"},
               hint="Server: <name>.postgres.database.azure.com. Flexible Server: the user name is the plain login; Single Server: login@servername."),
    PresetInfo(id="azure-mysql", label="Azure Database for MySQL", dialect="mysql", port=3306, options={"ssl": True, "ssl_verify": True},
               hint="Server: <name>.mysql.database.azure.com. TLS is required and the server certificate is verified."),
]


@router.get("/presets", response_model=list[PresetInfo])
def presets() -> list[PresetInfo]:
    """Ready-made settings for cloud databases (the same connectors, with the encryption they demand)."""
    return PRESETS


@router.get("", response_model=list[ConnectionOut])
def list_connections(role: Optional[str] = None, ctx: AppContext = Depends(get_ctx)) -> list[ConnectionOut]:
    """Saved connections; `?role=source` or `?role=target` narrows the list."""
    return [_out(ctx, r) for r in ctx.connections.list() if role is None or r.role == role]


@router.post("", response_model=ConnectionOut, status_code=201)
def save_connection(body: ConnectionIn, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> ConnectionOut:
    """Create or update a connection. The password (if given) is kept in server memory only; or name an environment variable."""
    data = body.model_dump(exclude={"password"})
    try:
        record = ctx.connections.save(data, user, password=body.password)
    except ConnectionInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _out(ctx, record)


@router.delete("/{name}")
def delete_connection(name: str, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> dict:
    """Forget a connection. Tables already imported from it keep their snapshots and lineage."""
    if not ctx.connections.delete(name):
        raise HTTPException(404, f"connection '{name}' does not exist")
    return {"deleted": name}


@router.post("/test", response_model=ConnectionTestResult)
def test_connection(body: ConnectionTestIn, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> ConnectionTestResult:
    """Connect, read the server version and count the schemas this account can see. Nothing is saved or changed."""
    try:
        if body.name:
            spec = ctx.connections.spec_for(body.name, password_override=body.password)
        else:
            if not (body.dialect and body.host and body.user):
                raise HTTPException(422, "give a saved connection name, or dialect, host and user")
            spec = ctx.connections.spec_inline(body.model_dump(exclude={"password", "name", "password_env"}), ctx.connections.resolve_password(body.password, body.password_env))
    except ConnectionInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ConnectorError as exc:
        return ConnectionTestResult(ok=False, detail=str(exc))
    try:
        if family(spec.dialect) == "storage":
            store = storage_for(spec)
            result = store.test()
            count = len(store.containers())
        else:
            connector = connector_for(spec)
            result = connector.test()
            count = len(connector.schemas())
    except HostKeyError as exc:
        return ConnectionTestResult(ok=False, detail=str(exc), host_key=exc.fingerprint)
    except ConnectorError as exc:
        return ConnectionTestResult(ok=False, detail=str(exc))
    return ConnectionTestResult(ok=True, detail=f"connected to {spec.host}:{spec.port} as {spec.user}", server_version=result["server_version"],
                                latency_ms=result["latency_ms"], schemas=count)


@router.post("/{name}/test-write", response_model=ConnectionTestResult)
def test_write(name: str, body: WriteTestIn, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_connection_admin)) -> ConnectionTestResult:
    """TARGET connections only: prove the account can create, fill, read and drop a table in a schema (a scratch table that is always removed)."""
    record = ctx.connections.get(name)
    if record is None:
        raise HTTPException(404, f"connection '{name}' does not exist")
    if record.role != "target":
        raise HTTPException(409, f"connection '{name}' is a source connection (read-only); only target connections are written to")
    try:
        detail = _connector(ctx, name).probe_write(body.schema_name)
    except ConnectorError as exc:
        return ConnectionTestResult(ok=False, detail=str(exc))
    return ConnectionTestResult(ok=True, detail=detail)


@router.get("/{name}/containers", response_model=list[str])
def containers(name: str, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> list[str]:
    """File storage: the containers / buckets (SFTP: the base directory) this account can see."""
    return _guard(lambda: _storage(ctx, name).containers())


@router.get("/{name}/files", response_model=list[StorageFile])
def files(name: str, container: str, prefix: str = "", ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> list[StorageFile]:
    """File storage: the folders and files directly under `prefix` (one level, folders first)."""
    found = _guard(lambda: _storage(ctx, name).list(container, prefix))
    return [StorageFile(name=e.name, key=e.key, size=e.size, modified=e.modified, is_dir=e.is_dir) for e in found]


@router.get("/{name}/schemas", response_model=list[str])
def schemas(name: str, include_system: bool = False, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> list[str]:
    return _guard(lambda: _connector(ctx, name).schemas(include_system))


@router.get("/{name}/tables", response_model=list[DbTable])
def tables(name: str, schema_name: str, views: bool = True, filter: str = "", ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> list[DbTable]:
    found = _guard(lambda: _connector(ctx, name).tables(schema_name, include_views=views, name_filter=filter))
    return [DbTable(schema_name=t.schema, name=t.name, kind=t.kind, row_estimate=t.row_estimate) for t in found]


@router.get("/{name}/columns", response_model=list[DbColumn])
def columns(name: str, schema_name: str, table: str, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> list[DbColumn]:
    cols = _guard(lambda: _connector(ctx, name).columns(schema_name, table))
    return [DbColumn(name=c.name, data_type=c.data_type, length=c.length, nullable=c.nullable, is_primary_key=c.is_primary_key, position=c.position) for c in cols]


@router.get("/{name}/preview", response_model=DbPreview)
def preview(name: str, schema_name: str, table: str, limit: int = 20, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> DbPreview:
    cols, rows = _guard(lambda: _connector(ctx, name).preview(schema_name, table, limit))
    return DbPreview(columns=[DbColumn(name=c.name, data_type=c.data_type, length=c.length, nullable=c.nullable, is_primary_key=c.is_primary_key, position=c.position) for c in cols], rows=rows)
