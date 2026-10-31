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


from src.connectors.fingerprint import Fingerprint  # noqa: E402


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
    values_verified: bool = False       # every cell read back from the new table equals what was sent (checked before the commit)
    cells_compared: int = 0
    value_digest: str = ""              # fingerprint of the table's content, for the record and the certificate


@dataclass
class RollbackOutcome:
    removed: int                        # rows of the run that were deleted
    remaining: int                      # rows left in the table (other runs' rows)
    table_dropped: bool                 # the table held nothing but this run, so it was removed too
    seconds: float


@dataclass
class MergeOutcome:
    inserted: int                       # records that were not in the table yet
    updated: int                        # records that were replaced (their old version went to the before-image)
    deleted: int                        # records removed on request (also kept in the before-image)
    rows_before: int
    rows_after: int                     # COUNT(*) before the commit; equals rows_before + inserted - deleted
    before_rows: int                    # rows handed to the before-image sink
    seconds: float
    values_verified: bool = False
    cells_compared: int = 0
    value_digest: str = ""


@dataclass
class MergeRollbackOutcome:
    removed: int                        # rows of the run that were deleted again
    restored: int                       # before-image rows put back
    rows_before: int
    rows_after: int
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
                   progress: Optional[Callable[[int], None]] = None, verify_values: bool = True,
                   owned_marker_columns: Sequence[str] = ()) -> LoadOutcome:
        """Create `schema.table` and fill it, all-or-nothing. `create` refuses an existing table; `replace` swaps it for the new one.
        `owned_marker_columns`: a `replace` only drops an existing table that has all of these columns, i.e. one an earlier delivery of
        this application created. Any other table (somebody's data, a typo in the table name) is refused and left untouched.
        The row count of the new table is read back inside the same transaction and must equal the rows sent (and `expected_rows`),
        otherwise nothing is kept. Where the product can roll DDL back (PostgreSQL, SQL Server) a replace is one transaction. Where it
        cannot (Oracle, MySQL) the new table is built under a temporary name and verified FIRST; only then is the old table dropped and
        the new one renamed into place, so a failed replace never costs the table that was already there.

        `verify_values` (default on): before the commit every value is read back from the new table and fingerprinted against what was sent,
        so a delivery that arrives with the right number of rows but altered values (a character set that cannot hold an umlaut, a value cut
        short) is refused and rolled back, naming the columns that differ."""
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
        sent_fp = Fingerprint([c.name for c in cols], [i for i, c in enumerate(cols) if c.name in primary_key] or [0])
        with self.session(read_only=False) as conn:
            self._check_schema(conn, schema)
            existing = {str(r[0]) for r in self._query(conn, *self._tables_sql(schema))}
            replaced = table in existing
            if replaced and mode == "create":
                raise ConnectorError(f"{schema}.{table} already exists; load into a new table name, or choose replace to drop it first")
            if replaced and owned_marker_columns:
                held = {c.name.lower() for c in self._columns(conn, schema, table)}
                missing = [m for m in owned_marker_columns if self.natural_case(m).lower() not in held]
                if missing:
                    raise ConnectorError(f"{schema}.{table} was not created by a delivery of this application (it has no {', '.join(missing)} column), "
                                         "so replace will not drop it; choose a new table name, or drop the table yourself if it really is disposable")
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
                        sent_fp.add(batch[-1])
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
                if verify_values:
                    held = self._fingerprint(conn, schema, work, cols, primary_key)
                    wrong = sent_fp.differing_columns(held)
                    if wrong:
                        raise ConnectorError(
                            f"value check failed: the database holds different values than were sent in column(s) {', '.join(wrong)}"
                            f" ({held.rows} row(s) read back); a character set or column type that cannot hold the data is the usual cause; nothing was kept")
                conn.commit()
                if swap:                                                         # the new table is complete and verified: now swap it in
                    self._drop(conn, schema, table)
                    self._exec(conn, self._rename_sql(schema, work, table))
                    conn.commit()
                return LoadOutcome(ddl=ddl, rows_inserted=sent, rows_in_target=in_target, replaced_existing=replaced, seconds=time.monotonic() - started,
                                   values_verified=verify_values, cells_compared=sent_fp.cells if verify_values else 0,
                                   value_digest=sent_fp.digest if verify_values else "")
            except ConnectorError:
                self._undo(conn, schema, work, created)
                raise
            except Exception as exc:  # noqa: BLE001 - driver errors mid-load
                self._undo(conn, schema, work, created)
                raise ConnectorError(f"the load failed and was rolled back: {self._scrub(exc)}") from None

    def _fingerprint(self, conn: Any, schema: str, table: str, cols: Sequence[Any], key_columns: Sequence[str]) -> Fingerprint:
        """Read every value of `cols` from the table over `conn` and fingerprint it (names are the database's own spelling)."""
        names = [c.name for c in cols]
        _, stream = self._open_stream(conn, f"SELECT {', '.join(self.quote(n) for n in names)} FROM {self.quote(schema)}.{self.quote(table)}")
        fp = Fingerprint(names, [i for i, n in enumerate(names) if n in key_columns] or [0])
        try:
            for row in stream:
                fp.add(row)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dropped connection while reading back
            raise ConnectorError(f"the values could not be read back: {self._scrub(exc)}") from None
        return fp

    def fingerprint_table(self, schema: str, table: str, columns: list[str], key_columns: list[str]) -> Fingerprint:
        """The content fingerprint of an existing table (read-only), to compare with what was delivered into it."""
        return self._retrying(lambda: self._fingerprint_once(schema, table, columns, key_columns))

    def _fingerprint_once(self, schema: str, table: str, columns: list[str], key_columns: list[str]) -> Fingerprint:
        with self.session() as conn:
            meta = self._check_target(conn, schema, table, columns)
            return self._fingerprint(conn, schema, table, meta, key_columns)

    # ------------------------------------------------------------------ merge (delta) deliveries
    def _in_list(self, n: int, start: int = 1) -> str:
        return ", ".join(self._placeholder(start + i) for i in range(n))

    def _fetch_by_keys(self, conn: Any, full: str, names: list[str], key: str, keys: Sequence[Any]) -> list[tuple]:
        """The rows of the table whose key is in `keys` (a bound IN list), in the column order of `names`."""
        sql = f"SELECT {', '.join(self.quote(n) for n in names)} FROM {full} WHERE {self.quote(key)} IN ({self._in_list(len(keys))})"
        cur = conn.cursor()
        try:
            cur.execute(sql, tuple(keys))
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    def _insert_chunk(self, conn: Any, insert_sql: str, chunk: list[tuple]) -> None:
        """Insert with a cursor of its own: some drivers (pytds) invalidate an older cursor when another statement runs on the connection."""
        cur = conn.cursor()
        try:
            self._insert_many(cur, insert_sql, chunk)
        finally:
            cur.close()

    def _delete_by_keys(self, conn: Any, full: str, key: str, keys: Sequence[Any]) -> None:
        cur = conn.cursor()
        try:
            cur.execute(f"DELETE FROM {full} WHERE {self.quote(key)} IN ({self._in_list(len(keys))})", tuple(keys))
        finally:
            cur.close()

    def merge_table(self, schema: str, table: str, cols: list[ColumnDef], key_column: str, rows: Iterable[Sequence[Any]], delete_keys: Iterable[Any] = (),
                    batch_size: int = 300, before_sink: Optional[Callable[[list[tuple]], None]] = None, verify_values: bool = True,
                    expected_rows: Optional[int] = None) -> MergeOutcome:
        """Apply a delta to a table that an earlier delivery created: for every record, replace the row with the same key or insert it, and
        delete the keys in `delete_keys`. ONE transaction, committed only when (1) the row count after equals the count before plus the new
        records minus the deleted ones, (2) every delivered record can be read back with exactly the values that were sent (when
        `verify_values`), and (3) `before_sink` has received a copy of every row that was replaced or deleted, so the merge can be undone
        exactly. Any failure rolls everything back; this is DML only, so that holds on every dialect."""
        table = self.natural_case(table)
        cols = [ColumnDef(self.natural_case(c.name), c.kind, c.length, c.nullable) for c in cols]
        names = [c.name for c in cols]
        key = self.natural_case(key_column)
        if key not in names:
            raise ConnectorError(f"the key column {key} is not among the delivered columns")
        kidx = names.index(key)
        started = time.monotonic()
        batch_size = max(1, min(int(batch_size), 500))                  # SQL Server allows 2100 parameters per statement; stay far below
        sent_fp = Fingerprint(names, [kidx])
        sent_keys: list[Any] = []
        inserted = updated = deleted = before_n = 0
        with self.session(read_only=False) as conn:
            self._check_schema(conn, schema)
            self._check_target(conn, schema, table, names)
            held_names = {c.name for c in self._columns(conn, schema, table)}
            if held_names != set(names):
                raise ConnectorError(f"{schema}.{table} has different columns than this delivery ("
                                     f"{', '.join(sorted(held_names ^ set(names)))}): it cannot be merged into")
            full = f"{self.quote(schema)}.{self.quote(table)}"
            insert = self.insert_sql(schema, table, cols)
            try:
                rows_before = int(self._query(conn, f"SELECT COUNT(*) FROM {full}")[0][0])
                def take_before(old: list[tuple]) -> None:
                    nonlocal before_n
                    if old and before_sink:
                        before_sink([tuple(to_text(v) for v in r) for r in old])
                    before_n += len(old)

                chunk: list[tuple] = []

                def flush() -> None:
                    nonlocal inserted, updated
                    if not chunk:
                        return
                    keys = [r[kidx] for r in chunk]
                    old = self._fetch_by_keys(conn, full, names, key, keys)
                    take_before(old)
                    if old:
                        self._delete_by_keys(conn, full, key, keys)
                    self._insert_chunk(conn, insert, chunk)
                    updated += len(old)
                    inserted += len(chunk) - len(old)
                    chunk.clear()

                for row in rows:
                    row = tuple(row)
                    sent_fp.add(row)
                    sent_keys.append(row[kidx])
                    chunk.append(row)
                    if len(chunk) >= batch_size:
                        flush()
                flush()
                pending: list[Any] = []
                for k in delete_keys:
                    pending.append(k)
                    if len(pending) >= batch_size:
                        old = self._fetch_by_keys(conn, full, names, key, pending)
                        take_before(old)
                        self._delete_by_keys(conn, full, key, pending)
                        deleted += len(old)
                        pending = []
                if pending:
                    old = self._fetch_by_keys(conn, full, names, key, pending)
                    take_before(old)
                    self._delete_by_keys(conn, full, key, pending)
                    deleted += len(old)
                rows_after = int(self._query(conn, f"SELECT COUNT(*) FROM {full}")[0][0])
                if rows_after != rows_before + inserted - deleted:
                    raise ConnectorError(f"row count check failed: the table held {rows_before} row(s), {inserted} were added and {deleted} removed, "
                                         f"it now holds {rows_after}; nothing was kept")
                if expected_rows is not None and inserted + updated != expected_rows:
                    raise ConnectorError(f"{inserted + updated} record(s) were applied but {expected_rows} were expected; nothing was kept")
                if verify_values:
                    held = Fingerprint(names, [kidx])
                    for i in range(0, len(sent_keys), batch_size):
                        for r in self._fetch_by_keys(conn, full, names, key, sent_keys[i:i + batch_size]):
                            held.add(r)
                    wrong = sent_fp.differing_columns(held)
                    if wrong:
                        raise ConnectorError("value check failed: the table holds different values than were sent in column(s) " + ", ".join(wrong)
                                             + f" ({held.rows} of {sent_fp.rows} record(s) read back); nothing was kept")
                conn.commit()
            except ConnectorError:
                self._undo(conn, schema, table, False)
                raise
            except Exception as exc:  # noqa: BLE001
                self._undo(conn, schema, table, False)
                raise ConnectorError(f"the merge failed and was rolled back: {self._scrub(exc)}") from None
        return MergeOutcome(inserted=inserted, updated=updated, deleted=deleted, rows_before=rows_before, rows_after=rows_after, before_rows=before_n,
                            seconds=time.monotonic() - started, values_verified=verify_values, cells_compared=sent_fp.cells if verify_values else 0,
                            value_digest=sent_fp.digest if verify_values else "")

    def rollback_merge(self, schema: str, table: str, cols: list[ColumnDef], key_column: str, run_column: str, run_id: int,
                       before_rows: Iterable[Sequence[Any]], expected_mine: int, expected_before: int, batch_size: int = 300) -> MergeRollbackOutcome:
        """Undo a merge: delete every row the run wrote (new and replaced alike), then put the before-image back (the old versions of the
        replaced rows and the rows it deleted). One transaction, committed only when the table holds the rows the merge left (a changed
        table is not rolled back automatically) and the counts after the restore add up."""
        table = self.natural_case(table)
        cols = [ColumnDef(self.natural_case(c.name), c.kind, c.length, c.nullable) for c in cols]
        names = [c.name for c in cols]
        run_column = self.natural_case(run_column)
        started = time.monotonic()
        with self.session(read_only=False) as conn:
            self._check_schema(conn, schema)
            self._check_target(conn, schema, table, names)
            full = f"{self.quote(schema)}.{self.quote(table)}"
            run_q = self.quote(run_column)
            try:
                total = int(self._query(conn, f"SELECT COUNT(*) FROM {full}")[0][0])
                mine = int(self._query(conn, f"SELECT COUNT(*) FROM {full} WHERE {run_q} = {int(run_id)}")[0][0])
                if mine != expected_mine:
                    raise ConnectorError(f"{schema}.{table} holds {mine} row(s) of run {run_id} but the merge wrote {expected_mine}; "
                                         "it has been changed since, so it is not rolled back automatically")
                cur = conn.cursor()
                try:
                    cur.execute(f"DELETE FROM {full} WHERE {run_q} = {int(run_id)}")
                    insert = self.insert_sql(schema, table, cols)
                    kinds = [c.kind for c in cols]
                    restored = 0
                    batch: list[tuple] = []
                    for row in before_rows:                                        # the before-image is text: ints back to ints, empty back to NULL
                        batch.append(tuple((None if v in (None, "") else (int(v) if kinds[i] == "int" else v)) for i, v in enumerate(row)))
                        if len(batch) >= batch_size:
                            self._insert_many(cur, insert, batch)
                            restored += len(batch)
                            batch = []
                    if batch:
                        self._insert_many(cur, insert, batch)
                        restored += len(batch)
                finally:
                    cur.close()
                if restored != expected_before:
                    raise ConnectorError(f"the before-image holds {restored} row(s) but {expected_before} were expected; nothing was changed")
                after = int(self._query(conn, f"SELECT COUNT(*) FROM {full}")[0][0])
                if after != total - mine + restored:
                    raise ConnectorError(f"the rollback check failed (before {total}, removed {mine}, restored {restored}, after {after}); nothing was changed")
                conn.commit()
            except ConnectorError:
                self._undo(conn, schema, table, False)
                raise
            except Exception as exc:  # noqa: BLE001
                self._undo(conn, schema, table, False)
                raise ConnectorError(f"the rollback failed and was undone: {self._scrub(exc)}") from None
        return MergeRollbackOutcome(removed=mine, restored=restored, rows_before=total, rows_after=after, seconds=time.monotonic() - started)

    def rollback_delivery(self, schema: str, table: str, run_column: str, run_id: int, expected_rows: Optional[int] = None) -> RollbackOutcome:
        """Undo a delivery: delete exactly the rows that carry `run_id` in `run_column`, in ONE transaction that is only committed when the
        count before, the number removed and the count after agree (and, when `expected_rows` is given, when that many rows were there).
        If the table then holds nothing it is dropped as well, so a rolled-back delivery leaves no empty table behind. Rows of other runs
        are never touched."""
        started = time.monotonic()
        with self.session(read_only=False) as conn:
            self._check_schema(conn, schema)
            self._check_target(conn, schema, table, [run_column])
            full = f"{self.quote(schema)}.{self.quote(table)}"
            col = self.quote(run_column)
            total = int(self._query(conn, f"SELECT COUNT(*) FROM {full}")[0][0])
            mine = int(self._query(conn, f"SELECT COUNT(*) FROM {full} WHERE {col} = {int(run_id)}")[0][0])
            if mine == 0:
                raise ConnectorError(f"{schema}.{table} holds no rows of run {run_id}: nothing to roll back")
            if expected_rows is not None and mine != expected_rows:
                raise ConnectorError(f"{schema}.{table} holds {mine} row(s) of run {run_id} but {expected_rows} were delivered; "
                                     "it has been changed since, so it is not rolled back automatically")
            try:
                cur = conn.cursor()
                try:
                    cur.execute(f"DELETE FROM {full} WHERE {col} = {int(run_id)}")
                finally:
                    cur.close()
                left = int(self._query(conn, f"SELECT COUNT(*) FROM {full}")[0][0])
                gone = int(self._query(conn, f"SELECT COUNT(*) FROM {full} WHERE {col} = {int(run_id)}")[0][0])
                if gone != 0 or left != total - mine:
                    raise ConnectorError(f"the rollback check failed (before {total}, run rows {mine}, after {left}, run rows still there {gone}); nothing was changed")
                conn.commit()
            except ConnectorError:
                self._undo(conn, schema, table, False)
                raise
            except Exception as exc:  # noqa: BLE001
                self._undo(conn, schema, table, False)
                raise ConnectorError(f"the rollback failed and was undone: {self._scrub(exc)}") from None
            dropped = False
            if left == 0:
                self._drop(conn, schema, table)
                conn.commit()
                dropped = True
        return RollbackOutcome(removed=mine, remaining=left, table_dropped=dropped, seconds=time.monotonic() - started)

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
