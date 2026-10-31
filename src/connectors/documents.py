"""Read-only connector to a MongoDB database: collections become flat, all-text tables so that landing, profiling, mapping,
validation and the certificate work exactly as they do for files and SQL databases.

How documents become rows (and what that costs):

* One document = one row. A nested document becomes dotted columns (`address.city`), to a depth of 4 levels; deeper than that,
  and every array, is kept whole as JSON text in one column. Nothing is expanded into several rows, so the number of rows
  always equals the number of documents (the control total).
* A field a document does not have, and a null, both come out as an empty cell: the migration cannot tell them apart afterwards.
* The columns are the union of the fields of ALL documents, found while reading (a first pass writes the flattened rows to a
  temporary file, a second pass writes the CSV with the full header), so a field that only the last document has is not lost.
  There is a cap on the number of columns: a collection with thousands of distinct field names is a sign it is not a table.
* Two different paths that would give the same column name (a field literally called `a.b` next to `a: {b: ...}`) stop the extract.
* Values: ObjectId -> hex text, dates -> ISO text (UTC), Decimal128 -> plain decimal text, binary -> hex, booleans -> true/false.

Nothing is ever written. The account should only have the `read` role, and no command here can change data; there is no
snapshot isolation on a standalone server, so a collection that changes DURING the extract can differ from the count taken before
it: the landing rejects that (rows read must equal the count).
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from src.connectors.base import (
    CHECK_EVERY_BYTES, ColumnMeta, ConnectionSpec, ConnectorAuthError, ConnectorError, ExtractResult, TableInfo, to_text)

MAX_DEPTH = 4
MAX_COLUMNS = 500
SAMPLE_DOCUMENTS = 1000
BATCH = 2000


def _scalar(value: Any) -> str:
    """The text of one leaf value."""
    try:
        from bson.decimal128 import Decimal128
        from bson.objectid import ObjectId
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, Decimal128):
            return format(value.to_decimal(), "f")
    except ImportError:  # pragma: no cover - pymongo always brings bson
        pass
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return to_text(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return _scalar(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return _scalar(value) if not isinstance(value, (str, int, float, bool)) else value


def flatten(doc: dict, prefix: str = "", depth: int = 0, out: Optional[dict[str, str]] = None) -> dict[str, str]:
    """One document -> {column: text}. Raises ConnectorError when two paths give the same column name."""
    out = {} if out is None else out
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and depth < MAX_DEPTH:
            flatten(value, name + ".", depth + 1, out)
            continue
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, default=_json_default, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        elif value is None:
            text = ""
        else:
            text = _scalar(value)
        if name in out:
            raise ConnectorError(f"two fields would both become the column '{name}' in one document (a field name that contains a '.' next to a nested document); "
                                 f"rename one of them in the source or leave the collection out")
        out[name] = text
    return out


def _type_names(doc: dict, prefix: str = "", depth: int = 0, out: Optional[dict[str, str]] = None) -> dict[str, str]:
    out = {} if out is None else out
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and depth < MAX_DEPTH:
            _type_names(value, name + ".", depth + 1, out)
        else:
            out[name] = "null" if value is None else "array" if isinstance(value, (list, tuple)) else "object" if isinstance(value, dict) else type(value).__name__
    return out


class MongoConnector:
    """Same read API as the SQL connectors (test, schemas, tables, columns, count, preview, extract_csv): a 'schema' is a database
    and a 'table' is a collection. It has no write methods on purpose: a document database is a source only."""

    dialect, default_port, system_type, label = "mongodb", 27017, "MONGODB", "MongoDB"
    transactional_ddl = False

    def __init__(self, spec: ConnectionSpec, connect: Optional[Callable[[], Any]] = None) -> None:
        self.spec = spec
        self._connect = connect or self._open

    # ------------------------------------------------------------------ connection
    def _open(self) -> Any:
        from pymongo import MongoClient
        s = self.spec
        timeout = s.connect_timeout * 1000
        tls = bool(s.options.get("tls") or s.options.get("ssl"))
        extra: dict[str, Any] = {"tls": True, "tlsCAFile": s.options["tls_ca_file"]} if tls and s.options.get("tls_ca_file") else {"tls": True} if tls else {}
        return MongoClient(host=s.host, port=s.port, username=s.user or None, password=s.password or None,
                           authSource=str(s.options.get("auth_source") or s.database or "admin"), serverSelectionTimeoutMS=timeout,
                           connectTimeoutMS=timeout, socketTimeoutMS=max(timeout * 6, 60000), appname="erp-migration-accelerator", retryWrites=False, **extra)

    def _scrub(self, exc: Any) -> str:
        text = str(exc)
        return text.replace(self.spec.password, "***") if self.spec.password else text

    def _translate(self, exc: Exception) -> ConnectorError:
        from pymongo import errors
        if isinstance(exc, ConnectorError):
            return exc
        if isinstance(exc, errors.OperationFailure) and (getattr(exc, "code", None) in (18, 13) or "uthentication" in str(exc)):
            return ConnectorAuthError("MongoDB rejected the user name or password (or the authentication database is wrong)")
        if isinstance(exc, errors.ServerSelectionTimeoutError):
            return ConnectorError(f"could not reach MongoDB at {self.spec.host}:{self.spec.port}: check the host, port, firewall and TLS setting")
        return ConnectorError(f"MongoDB could not answer the request: {self._scrub(exc)[:300]}")

    def _client(self) -> Any:
        try:
            return self._connect()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        client = self._client()
        try:
            return fn(client)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None
        finally:
            client.close()

    # ------------------------------------------------------------------ browsing
    def test(self) -> dict[str, Any]:
        started = time.monotonic()
        version = self._run(lambda c: str(c.server_info().get("version", "")))
        return {"ok": True, "server_version": f"MongoDB {version}", "latency_ms": int((time.monotonic() - started) * 1000)}

    def schemas(self, include_system: bool = False) -> list[str]:
        def go(client: Any) -> list[str]:
            try:
                found = [d["name"] for d in client.admin.command("listDatabases", nameOnly=True, authorizedDatabases=True)["databases"]]
            except Exception:  # noqa: BLE001 - an account without listDatabases: the one database it was given
                found = [self.spec.database] if self.spec.database else []
            return sorted(n for n in found if include_system or n not in ("admin", "local", "config"))
        return self._run(go)

    def _collections(self, client: Any, database: str) -> dict[str, str]:
        return {c["name"]: ("VIEW" if c.get("type") == "view" else "TABLE") for c in client[database].list_collections() if not c["name"].startswith("system.")}

    def tables(self, schema: str, include_views: bool = True, name_filter: str = "", limit: int = 2000) -> list[TableInfo]:
        def go(client: Any) -> list[TableInfo]:
            needle = name_filter.strip().lower()
            out = []
            for name, kind in sorted(self._collections(client, schema).items()):
                if (include_views or kind == "TABLE") and (not needle or needle in name.lower()):
                    try:
                        estimate = client[schema][name].estimated_document_count() if kind == "TABLE" else None
                    except Exception:  # noqa: BLE001
                        estimate = None
                    out.append(TableInfo(schema, name, kind, estimate))
            return out[:limit]
        return self._run(go)

    def _check(self, client: Any, schema: str, table: str) -> Any:
        if table not in self._collections(client, schema):
            raise ConnectorError(f"collection {schema}.{table} was not found, or this account cannot see it")
        return client[schema][table]

    def _sample_columns(self, coll: Any) -> list[ColumnMeta]:
        types: dict[str, set[str]] = {}
        for doc in coll.find({}, limit=SAMPLE_DOCUMENTS):
            for name, kind in _type_names(doc).items():
                types.setdefault(name, set()).add(kind)
        if not types:
            return []
        names = ["_id"] + [n for n in types if n != "_id"]
        if len(names) > MAX_COLUMNS:
            raise ConnectorError(f"{coll.full_name} has more than {MAX_COLUMNS} distinct field names in its first {SAMPLE_DOCUMENTS} documents; it is not table-shaped")
        return [ColumnMeta(name=n, data_type="|".join(sorted(types.get(n, {"objectid"}))), length=None, nullable=n != "_id", is_primary_key=n == "_id", position=i + 1)
                for i, n in enumerate(names)]

    def columns(self, schema: str, table: str) -> list[ColumnMeta]:
        """From a sample (the first documents): the extract reads every document and may find more fields."""
        def go(client: Any) -> list[ColumnMeta]:
            cols = self._sample_columns(self._check(client, schema, table))
            if not cols:
                raise ConnectorError(f"{schema}.{table} has no documents")
            return cols
        return self._run(go)

    def count(self, schema: str, table: str) -> int:
        return self._run(lambda c: int(self._check(c, schema, table).count_documents({})))

    def preview(self, schema: str, table: str, limit: int = 20) -> tuple[list[ColumnMeta], list[list[str]]]:
        limit = max(1, min(int(limit), 200))

        def go(client: Any) -> tuple[list[ColumnMeta], list[list[str]]]:
            coll = self._check(client, schema, table)
            cols = self._sample_columns(coll)
            rows = [flatten(d) for d in coll.find({}, limit=limit)]
            return cols, [[r.get(c.name, "") for c in cols] for r in rows]
        return self._run(go)

    # ------------------------------------------------------------------ extraction
    def extract_csv(self, schema: str, table: str, columns: Optional[list[str]], out_path: Path, max_bytes: int,
                    progress: Optional[Callable[[int], None]] = None) -> ExtractResult:
        started = time.monotonic()

        def go(client: Any) -> ExtractResult:
            coll = self._check(client, schema, table)
            source_count = int(coll.count_documents({}))
            rows = (flatten(doc) for doc in coll.find({}, batch_size=BATCH).sort("_id", 1))
            count, names = rows_to_csv(rows, columns, out_path, max_bytes, f"{schema}.{table}", progress)
            return ExtractResult(path=out_path, rows=count, source_count=source_count, bytes_written=out_path.stat().st_size, columns=names,
                                 seconds=time.monotonic() - started)
        return self._run(go)


def rows_to_csv(rows: Iterator[dict[str, str]], columns: Optional[list[str]], out_path: Path, max_bytes: int, label: str,
                progress: Optional[Callable[[int], None]] = None) -> tuple[int, list[str]]:
    """Flat rows (documents already flattened, in a stable order) -> one CSV with the union of their fields as the header. Two passes, because a field that only the last
    document has must still be a column: the rows are spooled to a compressed temporary file first. -> (rows written, column names). Shared by every document-database source."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    too_big = f"the extract is larger than the {max_bytes // 1048576} MB limit; select fewer columns or raise MIGRATION_MAX_EXTRACT_MB"
    wanted = list(columns) if columns else None
    order: dict[str, None] = {"_id": None}
    count = raw = 0
    next_check = CHECK_EVERY_BYTES
    with tempfile.TemporaryDirectory() as tmp:
        spool = Path(tmp) / "rows.jsonl.gz"
        with gzip.open(spool, "wt", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                if wanted is not None:
                    row = {k: v for k, v in row.items() if k in wanted}
                for key in row:
                    if key not in order:
                        order[key] = None
                        if len(order) > MAX_COLUMNS:
                            raise ConnectorError(f"{label} has more than {MAX_COLUMNS} distinct fields; select the columns you need")
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
                count += 1
                raw += sum(len(v) for v in row.values()) + len(row)
                if raw >= next_check or count % 20000 == 0:
                    fh.flush()
                    if spool.stat().st_size > max_bytes * 4:        # the spool is JSON: allow for its overhead; the CSV is checked below
                        raise ConnectorError(too_big)
                    next_check = raw + CHECK_EVERY_BYTES
                    if progress:
                        progress(count)
        if wanted is not None:
            missing = [c for c in wanted if c not in order]
            if missing:
                raise ConnectorError(f"column(s) not found in {label}: {', '.join(missing)}")
            names = wanted
        else:
            names = list(order)
        try:
            with gzip.open(spool, "rt", encoding="utf-8") as src, gzip.open(out_path, "wt", encoding="utf-8", newline="") as dst:
                writer = csv.writer(dst, lineterminator="\n")
                writer.writerow(names)
                for line in src:
                    row = json.loads(line)
                    writer.writerow([row.get(n, "") for n in names])
            if out_path.stat().st_size > max_bytes:
                raise ConnectorError(too_big)
        except Exception:
            out_path.unlink(missing_ok=True)
            raise
    return count, names
