"""Read-only connectors to legacy databases.

A connector knows how to (1) test a connection, (2) browse schemas, tables and columns, and (3) extract one table into a UTF-8
CSV that is then landed like any other file, so control totals, profiling, lineage and reconciliation work the same for files
and databases. Design rules:

* Nothing is ever written to the source. Sessions are opened read-only where the database supports it, and the account used
  should be a read-only account regardless.
* No SQL text comes from the user. Schema, table and column names are checked against the database's own catalog, then quoted
  for the dialect; the only statements run are the connector's own metadata queries and a generated SELECT.
* The row count (the control total) and the extract are taken inside one transaction, so a table that changes during the
  extract cannot make them disagree silently.
* Streaming and bounded: rows are fetched in batches and written straight to a (gzip) file; a size cap stops runaway extracts.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import re
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

FETCH_BATCH = 5000
CHECK_EVERY_BYTES = 8 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 10


SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")   # names we create: letters, digits, underscore; 30 chars is Oracle's old limit


class ConnectorError(Exception):
    """The database could not be reached, browsed or read (message is safe to show to users)."""


class ConnectorAuthError(ConnectorError):
    """The database rejected the credentials."""


@dataclass(frozen=True)
class ConnectionSpec:
    dialect: str
    host: str
    port: int
    database: str                       # database name; Oracle: service name
    user: str
    password: str = field(default="", repr=False)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def connect_timeout(self) -> int:
        return int(self.options.get("connect_timeout") or DEFAULT_CONNECT_TIMEOUT)


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    kind: str                           # TABLE | VIEW
    row_estimate: Optional[int] = None


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    data_type: str
    length: Optional[int]
    nullable: bool
    is_primary_key: bool
    position: int


@dataclass(frozen=True)
class ColumnDef:
    """A column of a table the application creates in a TARGET database."""
    name: str
    kind: str = "text"                  # text | int
    length: Optional[int] = None
    nullable: bool = True


@dataclass
class LoadOutcome:
    ddl: str
    rows_inserted: int
    rows_in_target: int                 # SELECT COUNT(*) on the new table, in the same transaction, before it was committed
    replaced_existing: bool
    seconds: float


@dataclass
class ExtractResult:
    path: Path
    rows: int
    source_count: int                   # SELECT count(*) taken in the same transaction as the extract
    bytes_written: int
    columns: list[str]
    seconds: float


def to_text(value: Any) -> str:
    """The exact text of a database value (blank for NULL). Numbers never use exponent notation; timestamps are ISO."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


_LENGTH = re.compile(r"\((\d+)")


def length_from_type(data_type: str) -> Optional[int]:
    m = _LENGTH.search(data_type or "")
    return int(m.group(1)) if m else None


class Connector(ABC):
    dialect: str = ""
    default_port: int = 0
    system_type: str = ""               # SystemType value used when a table is registered
    label: str = ""

    def __init__(self, spec: ConnectionSpec, connect: Optional[Callable[[], Any]] = None) -> None:
        self.spec = spec
        self._connect_override = connect          # tests inject a DB-API connection factory

    # ------------------------------------------------------------------ dialect hooks
    @abstractmethod
    def _connect(self) -> Any: ...
    @abstractmethod
    def quote(self, identifier: str) -> str: ...
    @abstractmethod
    def _schemas_sql(self, include_system: bool) -> tuple[str, tuple]: ...
    @abstractmethod
    def _tables_sql(self, schema: str) -> tuple[str, tuple]: ...
    @abstractmethod
    def _columns_sql(self, schema: str, table: str) -> tuple[str, tuple]: ...
    @abstractmethod
    def _limit_sql(self, select_sql: str, limit: int) -> str: ...
    @abstractmethod
    def _version(self, conn: Any) -> str: ...

    def _begin_read_only(self, conn: Any) -> None:       # best effort; subclasses tighten it
        return None

    def _retryable(self, exc: Exception) -> bool:        # a transient condition worth one more attempt (Oracle ORA-01466)
        return False

    def _retrying(self, fn: Callable[[], Any]) -> Any:
        """Reads are repeatable, so a transient refusal is retried a few times with a growing pause before it is reported."""
        for attempt in range(4):
            try:
                return fn()
            except ConnectorError as exc:
                if attempt == 3 or not self._retryable(exc):
                    raise
                time.sleep(1.5 * (attempt + 1))

    # ---- write support (targets only)
    transactional_ddl = False           # PostgreSQL and SQL Server roll CREATE TABLE back; Oracle and MySQL commit it at once

    def _type_sql(self, col: ColumnDef) -> str:
        raise ConnectorError(f"{self.dialect} cannot be used as a load target")

    def _placeholder(self, n: int) -> str:
        return "%s"

    def natural_case(self, name: str) -> str:
        return name

    def _table_suffix(self) -> str:
        return ""

    def _insert_many(self, cur: Any, sql: str, batch: list[tuple]) -> None:
        cur.executemany(sql, batch)

    def _stream(self, conn: Any, sql: str) -> tuple[list[str], Iterator[tuple]]:
        cur = conn.cursor()
        cur.execute(sql)
        names = [d[0] for d in cur.description]

        def rows() -> Iterator[tuple]:
            try:
                while True:
                    batch = cur.fetchmany(FETCH_BATCH)
                    if not batch:
                        return
                    yield from batch
            finally:
                cur.close()
        return names, rows()

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def session(self, read_only: bool = True) -> Iterator[Any]:
        try:
            conn = self._connect_override() if self._connect_override else self._connect()
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - drivers raise their own types; translate, never leak the password
            raise self._translate(exc) from None
        try:
            if read_only:                   # every source session; only the explicit write path below opts out
                try:
                    self._begin_read_only(conn)
                except ConnectorError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise ConnectorError(f"the database would not start a read-only session: {self._scrub(exc)}") from None
            yield conn
        finally:
            self._end_session(conn)

    def _end_session(self, conn: Any) -> None:
        """Nothing is ever committed on a source session; roll back (so locks are released) and close."""
        for step in (conn.rollback, conn.close):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass

    def _scrub(self, exc: Any) -> str:
        text = str(exc).strip()
        return (text.replace(self.spec.password, "***") if self.spec.password else text)[:240]

    def _open_stream(self, conn: Any, sql: str) -> tuple[list[str], Iterator[tuple]]:
        """Run the SELECT. Driver errors here (permission, timeout, encoding...) become ConnectorErrors like every other failure."""
        try:
            return self._stream(conn, sql)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"the database could not run the extract: {self._scrub(exc)}") from None

    def _translate(self, exc: Exception) -> ConnectorError:
        text = str(exc).replace(self.spec.password, "***") if self.spec.password else str(exc)
        lowered = text.lower()
        if any(k in lowered for k in ("requires encryption", "encryption enabled", "insecure transport", "ssl connection is required", "no encryption",
                                      "ssl/tls required", "ssl required", "must use ssl", "secure connection")):
            return ConnectorError("this server requires an encrypted (TLS) connection: switch encryption on in the connection options "
                                  f"(Azure databases always require it) - {text.strip()[:140]}")
        if any(k in lowered for k in ("certificate verify failed", "self-signed", "self signed", "does not match host name", "verify_cb", "unable to get local issuer",
                                      "hostname mismatch", "certificate is not trusted", "verify failed")):
            return ConnectorError("the server's TLS certificate was not trusted: give the CA certificate file that signed it, or check the host name "
                                  f"matches the certificate - {text.strip()[:140]}")
        if any(k in lowered for k in ("password", "authentication", "login failed", "ora-01017", "access denied", "invalid username")):
            return ConnectorAuthError(f"the database rejected the user name or password ({text.strip()[:160]})")
        return ConnectorError(f"cannot connect to {self.spec.host}:{self.spec.port}: {text.strip()[:240]}")

    def _query(self, conn: Any, sql: str, params: tuple = ()) -> list[tuple]:
        cur = conn.cursor()
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"the database could not answer the request: {self._scrub(exc)}") from None
        finally:
            cur.close()

    # ------------------------------------------------------------------ browsing
    def test(self) -> dict[str, Any]:
        started = time.monotonic()
        with self.session() as conn:
            version = self._version(conn)
        return {"ok": True, "server_version": version, "latency_ms": int((time.monotonic() - started) * 1000)}

    def schemas(self, include_system: bool = False) -> list[str]:
        with self.session() as conn:
            sql, params = self._schemas_sql(include_system)
            return sorted({str(r[0]) for r in self._query(conn, sql, params)})

    def tables(self, schema: str, include_views: bool = True, name_filter: str = "", limit: int = 2000) -> list[TableInfo]:
        with self.session() as conn:
            sql, params = self._tables_sql(schema)
            found = [TableInfo(schema, str(r[0]), str(r[1]), int(r[2]) if r[2] is not None and int(r[2]) >= 0 else None) for r in self._query(conn, sql, params)]
        needle = name_filter.strip().lower()
        out = [t for t in found if (include_views or t.kind == "TABLE") and (not needle or needle in t.name.lower())]
        return sorted(out, key=lambda t: t.name)[:limit]

    def columns(self, schema: str, table: str) -> list[ColumnMeta]:
        with self.session() as conn:
            return self._columns(conn, schema, table)

    def _columns(self, conn: Any, schema: str, table: str) -> list[ColumnMeta]:
        sql, params = self._columns_sql(schema, table)
        cols = [ColumnMeta(name=str(r[0]), data_type=str(r[1]), length=int(r[2]) if r[2] is not None else length_from_type(str(r[1])),
                           nullable=bool(r[3]), is_primary_key=bool(r[4]), position=int(r[5])) for r in self._query(conn, sql, params)]
        if not cols:
            raise ConnectorError(f"{schema}.{table} was not found, or this account cannot see its columns")
        return cols

    def _check_target(self, conn: Any, schema: str, table: str, columns: Optional[Iterable[str]]) -> list[ColumnMeta]:
        """Names must exist in the database's own catalog before they are ever put into SQL."""
        sql, params = self._tables_sql(schema)
        known = {str(r[0]) for r in self._query(conn, sql, params)}
        if table not in known:
            raise ConnectorError(f"table {schema}.{table} was not found, or this account cannot see it")
        meta = self._columns(conn, schema, table)
        if columns:
            wanted, have = list(columns), {c.name for c in meta}
            missing = [c for c in wanted if c not in have]
            if missing:
                raise ConnectorError(f"column(s) not in {schema}.{table}: {', '.join(missing)}")
            by = {c.name: c for c in meta}
            meta = [by[c] for c in wanted]
        return meta

    def _select(self, schema: str, table: str, cols: list[ColumnMeta]) -> str:
        return f"SELECT {', '.join(self.quote(c.name) for c in cols)} FROM {self.quote(schema)}.{self.quote(table)}"

    def count(self, schema: str, table: str) -> int:
        return self._retrying(lambda: self._count_once(schema, table))

    def _count_once(self, schema: str, table: str) -> int:
        with self.session() as conn:
            self._check_target(conn, schema, table, None)
            return int(self._query(conn, f"SELECT COUNT(*) FROM {self.quote(schema)}.{self.quote(table)}")[0][0])

    def preview(self, schema: str, table: str, limit: int = 20) -> tuple[list[ColumnMeta], list[list[str]]]:
        return self._retrying(lambda: self._preview_once(schema, table, limit))

    def _preview_once(self, schema: str, table: str, limit: int) -> tuple[list[ColumnMeta], list[list[str]]]:
        limit = max(1, min(int(limit), 200))
        with self.session() as conn:
            cols = self._check_target(conn, schema, table, None)
            names, rows = self._open_stream(conn, self._limit_sql(self._select(schema, table, cols), limit))
            data = [[to_text(v) for v in row] for row in rows][:limit]
        return cols, data

    # ------------------------------------------------------------------ extraction
    def extract_csv(self, schema: str, table: str, columns: Optional[list[str]], out_path: Path, max_bytes: int,
                    progress: Optional[Callable[[int], None]] = None) -> ExtractResult:
        """Write the table as a gzip UTF-8 CSV (header + all-text values). The control total is counted in the same
        transaction, immediately before the SELECT."""
        return self._retrying(lambda: self._extract_once(schema, table, columns, out_path, max_bytes, progress))

    def _extract_once(self, schema: str, table: str, columns: Optional[list[str]], out_path: Path, max_bytes: int,
                      progress: Optional[Callable[[int], None]] = None) -> ExtractResult:
        started = time.monotonic()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as conn:
            cols = self._check_target(conn, schema, table, columns)
            source_count = int(self._query(conn, f"SELECT COUNT(*) FROM {self.quote(schema)}.{self.quote(table)}")[0][0])
            names, rows = self._open_stream(conn, self._select(schema, table, cols))
            count = raw = 0
            next_check = CHECK_EVERY_BYTES
            too_big = f"the extract is larger than the {max_bytes // 1048576} MB limit; select fewer columns or raise MIGRATION_MAX_EXTRACT_MB"
            try:
                with gzip.open(out_path, "wt", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh, lineterminator="\n")
                    writer.writerow(names)
                    for row in rows:
                        fields = [to_text(v) for v in row]
                        writer.writerow(fields)
                        count += 1
                        raw += sum(len(f) for f in fields) + len(fields)
                        if raw >= next_check or count % 20000 == 0:      # by volume as well as by rows: a few huge LOB rows must hit the cap too
                            fh.flush()
                            if out_path.stat().st_size > max_bytes:
                                raise ConnectorError(too_big)
                            next_check = raw + CHECK_EVERY_BYTES
                            if progress:
                                progress(count)
                if out_path.stat().st_size > max_bytes:                   # and once more when everything has been written
                    raise ConnectorError(too_big)
            except ConnectorError:
                out_path.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001 - a dropped connection mid-stream
                out_path.unlink(missing_ok=True)
                raise ConnectorError(f"the extract was interrupted after {count:,} row(s): {self._scrub(exc)[:200]}") from None
        return ExtractResult(path=out_path, rows=count, source_count=source_count, bytes_written=out_path.stat().st_size,
                             columns=names, seconds=time.monotonic() - started)


    # ------------------------------------------------------------------ writing (target connections only)
    def check_new_name(self, name: str, what: str = "table") -> str:
        if not SAFE_NAME.match(name or ""):
            raise ConnectorError(f"{what} name '{name}' must start with a letter and use only letters, digits and '_' (up to 30 characters)")
        return self.natural_case(name)

    def create_table_sql(self, schema: str, table: str, cols: list[ColumnDef], primary_key: list[str]) -> str:
        lines = [f"  {self.quote(c.name)} {self._type_sql(c)}{'' if c.nullable and c.name not in primary_key else ' NOT NULL'}" for c in cols]
        if primary_key:
            lines.append(f"  PRIMARY KEY ({', '.join(self.quote(k) for k in primary_key)})")
        return f"CREATE TABLE {self.quote(schema)}.{self.quote(table)} (\n" + ",\n".join(lines) + f"\n){self._table_suffix()}"

    def insert_sql(self, schema: str, table: str, cols: list[ColumnDef]) -> str:
        names = ", ".join(self.quote(c.name) for c in cols)
        marks = ", ".join(self._placeholder(i + 1) for i in range(len(cols)))
        return f"INSERT INTO {self.quote(schema)}.{self.quote(table)} ({names}) VALUES ({marks})"

    def _exec(self, conn: Any, sql: str) -> None:
        cur = conn.cursor()
        try:
            cur.execute(sql)
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"the database refused the statement: {self._scrub(exc)}") from None
        finally:
            cur.close()

    def _drop(self, conn: Any, schema: str, table: str) -> None:
        self._exec(conn, f"DROP TABLE {self.quote(schema)}.{self.quote(table)}")

    def _check_schema(self, conn: Any, schema: str) -> None:
        sql, params = self._schemas_sql(True)
        if schema not in {str(r[0]) for r in self._query(conn, sql, params)}:
            raise ConnectorError(f"schema '{schema}' was not found, or this account cannot see it")

    def _rename_sql(self, schema: str, old: str, new: str) -> str:
        raise ConnectorError(f"{self.dialect} cannot swap tables")

    def load_table(self, schema: str, table: str, cols: list[ColumnDef], primary_key: list[str], rows: Iterable[Sequence[Any]],
                   mode: str = "create", batch_size: int = 1000, expected_rows: Optional[int] = None,
                   progress: Optional[Callable[[int], None]] = None) -> LoadOutcome:
        """Create `schema.table` and fill it, all-or-nothing. `create` refuses an existing table; `replace` swaps it for the new one.
        The row count of the new table is read back inside the same transaction and must equal the rows sent (and `expected_rows`),
        otherwise nothing is kept. Where the product can roll DDL back (PostgreSQL, SQL Server) a replace is one transaction. Where it
        cannot (Oracle, MySQL) the new table is built under a temporary name and verified FIRST; only then is the old table dropped and
        the new one renamed into place, so a failed replace never costs the table that was already there."""
        if mode not in ("create", "replace"):
            raise ConnectorError("mode must be 'create' or 'replace'")
        table = self.check_new_name(table)
        for c in cols:
            self.check_new_name(c.name, "column")
        cols = [ColumnDef(self.natural_case(c.name), c.kind, c.length, c.nullable) for c in cols]
        names = [c.name.lower() for c in cols]
        if len(set(names)) != len(names):
            raise ConnectorError("two columns would have the same name in this database: " + ", ".join(sorted({n for n in names if names.count(n) > 1})))
        primary_key = [self.natural_case(k) for k in primary_key]
        started = time.monotonic()
        ddl = self.create_table_sql(schema, table, cols, primary_key)
        with self.session(read_only=False) as conn:
            self._check_schema(conn, schema)
            existing = {str(r[0]) for r in self._query(conn, *self._tables_sql(schema))}
            replaced = table in existing
            if replaced and mode == "create":
                raise ConnectorError(f"{schema}.{table} already exists; load into a new table name, or choose replace to drop it first")
            work = table
            swap = replaced and not self.transactional_ddl
            if swap:
                work = self.natural_case(f"{table[:25]}_NEW")                   # fits the 30-character limit
                if work in existing:
                    raise ConnectorError(f"{schema}.{work} is left over from an interrupted replace; drop it, then try again")
            insert = self.insert_sql(schema, work, cols)
            created = False
            try:
                if replaced and not swap:
                    self._drop(conn, schema, table)                             # inside the transaction: a failure rolls it back
                self._exec(conn, self.create_table_sql(schema, work, cols, primary_key))
                created = True
                cur = conn.cursor()
                sent = 0
                try:
                    batch: list[tuple] = []
                    for row in rows:
                        batch.append(tuple(row))
                        if len(batch) >= batch_size:
                            self._insert_many(cur, insert, batch)
                            sent += len(batch)
                            batch = []
                            if progress:
                                progress(sent)
                    if batch:
                        self._insert_many(cur, insert, batch)
                        sent += len(batch)
                finally:
                    cur.close()
                in_target = int(self._query(conn, f"SELECT COUNT(*) FROM {self.quote(schema)}.{self.quote(work)}")[0][0])
                if in_target != sent or (expected_rows is not None and sent != expected_rows):
                    raise ConnectorError(f"row count check failed: {sent} row(s) were sent, the table holds {in_target}"
                                         + (f", {expected_rows} were expected" if expected_rows is not None else "") + "; nothing was kept")
                conn.commit()
                if swap:                                                         # the new table is complete and verified: now swap it in
                    self._drop(conn, schema, table)
                    self._exec(conn, self._rename_sql(schema, work, table))
                    conn.commit()
                return LoadOutcome(ddl=ddl, rows_inserted=sent, rows_in_target=in_target, replaced_existing=replaced, seconds=time.monotonic() - started)
            except ConnectorError:
                self._undo(conn, schema, work, created)
                raise
            except Exception as exc:  # noqa: BLE001 - driver errors mid-load
                self._undo(conn, schema, work, created)
                raise ConnectorError(f"the load failed and was rolled back: {self._scrub(exc)}") from None

    def _undo(self, conn: Any, schema: str, table: str, created: bool) -> None:
        """Leave nothing behind: roll back, and where DDL cannot be rolled back drop the table we created."""
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        if created and not self.transactional_ddl:
            try:
                self._drop(conn, schema, table)
                conn.commit()
            except Exception:  # noqa: BLE001
                pass

    def probe_write(self, schema: str) -> str:
        """Prove this account can create, fill, read and drop a table in `schema` (a scratch table that is always removed)."""
        name = self.natural_case("mig_probe_" + uuid.uuid4().hex[:8])
        cols = [ColumnDef("PROBE_ID", "int", nullable=False), ColumnDef("PROBE_TEXT", "text", 20)]
        with self.session(read_only=False) as conn:
            self._check_schema(conn, schema)
            made = False
            try:
                self._exec(conn, self.create_table_sql(schema, name, cols, ["PROBE_ID"]))
                made = True
                cur = conn.cursor()
                try:
                    cur.execute(self.insert_sql(schema, name, cols), (1, "ok"))
                finally:
                    cur.close()
                got = int(self._query(conn, f"SELECT COUNT(*) FROM {self.quote(schema)}.{self.quote(name)}")[0][0])
                if got != 1:
                    raise ConnectorError("the probe row could not be read back")
                return f"create, insert, read and drop all succeeded in schema {schema}"
            except ConnectorError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ConnectorError(f"this account cannot write in schema {schema}: {self._scrub(exc)[:200]}") from None
            finally:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                if made and not self.transactional_ddl:
                    try:
                        self._drop(conn, schema, name)
                        conn.commit()
                    except Exception:  # noqa: BLE001
                        pass
