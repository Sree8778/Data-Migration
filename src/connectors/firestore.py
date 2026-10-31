"""Connector to Google Cloud Firestore, the document database behind Firebase. As a SOURCE (read only) collections become flat, all-text tables so that landing, profiling,
mapping, validation and the certificate work exactly as they do for files, SQL databases and MongoDB. As a DELIVERY TARGET (see "Delivering into Firestore" below) a signed-off run is
written as one document per validated record.

How a connection is described (the connection form of every other database has no place for a key file, so the fields are used like this):

* host      firestore.googleapis.com (the real service). A loopback host (localhost / 127.0.0.1) with a port means the Firestore EMULATOR: no key is needed, nothing leaves the machine.
* database  the Firebase / Google Cloud PROJECT ID.
* user      the service account's e-mail (informational; the key itself says who it is).
* password  the service-account KEY, as the JSON text of the key file. Kept in server memory only, like every password; or name a server environment variable (password_env).
* option database_id   the Firestore database inside the project (default "(default)"). A project can have several.

How documents become rows is the same as for MongoDB (see documents.py): one document = one row, nested maps become dotted columns, arrays are kept whole as JSON text,
the columns are the union of the fields of all documents, and the document ID is the column `_id` (the primary key). Firestore types: timestamps -> ISO text (UTC, microsecond
precision: Firestore's nanoseconds are cut), GeoPoint -> `<column>.latitude` and `<column>.longitude`, a document reference -> its path, bytes -> hex.

As a source it never writes; it reads TOP-LEVEL collections only (a sub-collection is not followed); it does not listen for changes. Firestore bills per document read: a table
of N documents costs about N reads to extract, plus a few for the count. For a source give the account the "Cloud Datastore Viewer" role and nothing more.

Delivering into Firestore (a connection with role "target"; needs the "Cloud Datastore User" role):

* The schema is the Firestore database ("(default)"), the table is a COLLECTION, a record is a DOCUMENT. The document ID is the record's LEGACY_KEY (a "/" in a key is written as %2F,
  because an ID cannot contain one); every document also carries LEGACY_KEY and MIG_RUN_ID as fields, so a run's documents can always be found, counted and rolled back.
* All values are text except MIG_RUN_ID (an integer); an empty value is stored as null.
* "All or nothing" is kept by compensation, because Firestore has no multi-document transaction of this size: the documents are written in batches, the count and every VALUE are read
  back and compared with what was sent, and if anything is wrong the documents just written are deleted again ("nothing was kept"). Rollback deletes exactly the documents of a run.
* Only a NEW collection can be delivered into: documents cannot be swapped in one step, so "replace" and delta "merge" deliveries are refused, with the way out in the message.
* Cost: one write per document, and a delivery that verifies its values reads every document once more (a later "verify" again). The free plan allows 20,000 writes, 20,000 deletes
  and 50,000 reads a day.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from src.connectors.base import (
    ColumnDef, ColumnMeta, ConnectionSpec, ConnectorAuthError, ConnectorError, ExtractResult, LoadOutcome, MergeOutcome, MergeRollbackOutcome, RollbackOutcome, TableInfo)
from src.connectors.documents import MAX_COLUMNS, SAMPLE_DOCUMENTS, _type_names, flatten, rows_to_csv
from src.connectors.fingerprint import Fingerprint

GOOGLE_HOST = "firestore.googleapis.com"
LOOPBACK = ("localhost", "127.0.0.1", "::1")
DEFAULT_DATABASE_ID = "(default)"
BATCH = 1000
SCOPES = ["https://www.googleapis.com/auth/datastore"]
KEY_FIELDS = ("type", "project_id", "private_key", "client_email", "token_uri")
_ENV_LOCK = threading.Lock()
NEW_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")     # a collection or field this application creates: letters, digits and '_' (no '/', no '.', never a reserved __name__)
FREE_PLAN_WRITES = 20_000                                    # what the free (Spark) plan allows a day; the application cannot see which plan a project is on
WRITE_BATCH = 400                                             # documents per commit (the service allows 500 writes in one)
NO_REPLACE = ("Firestore can only receive a delivery into a NEW collection: documents cannot be swapped in one step, so 'replace' is not offered. "
              "Choose a new collection name, or roll the earlier delivery back first.")
NO_MERGE = ("delta (merge) deliveries into Firestore are not supported: deliver the full run into a new collection, or roll the earlier delivery back and deliver again")


def is_emulator(spec: ConnectionSpec) -> bool:
    return spec.host.lower() in LOOPBACK


def parse_key(text: str) -> dict[str, Any]:
    """The service-account key file, checked: what is wrong with it is said without ever repeating the key."""
    try:
        info = json.loads(text)
    except ValueError:
        raise ConnectorError("the key is not valid JSON: paste the whole content of the service-account key file (it starts with { and has a \"private_key\")") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ConnectorError("the key is not a service-account key: in the Google Cloud console create a key of type JSON for a service account (not an API key, not an OAuth client)")
    missing = [f for f in KEY_FIELDS if not info.get(f)]
    if missing:
        raise ConnectorError(f"the service-account key is incomplete (missing: {', '.join(missing)})")
    return info


def _plain(value: Any) -> Any:
    """A Firestore value as plain data that documents.flatten understands."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    name = type(value).__name__
    if name == "GeoPoint":
        return {"latitude": value.latitude, "longitude": value.longitude}
    if name == "DocumentReference":
        return value.path
    if name == "Vector":
        return list(value.to_map_value()["value"]) if hasattr(value, "to_map_value") else str(value)
    return value


def flatten_document(snapshot: Any) -> dict[str, str]:
    """One Firestore document -> {column: text}, the document ID first."""
    row = {"_id": str(snapshot.id)}
    for key, text in flatten(_plain(snapshot.to_dict() or {})).items():
        if key == "_id":
            raise ConnectorError(f"document {snapshot.id} has a field called '_id', which is the column that holds the document ID; rename it in the source or leave the collection out")
        row[key] = text
    return row


class _RowsFailed(ConnectorError):
    """Carries an error that did not come from Firestore through the connection wrapper, which would otherwise call it a Firestore error."""
    original: BaseException


class FirestoreConnector:
    """Same read API as the other connectors (test, schemas, tables, columns, count, preview, extract_csv): a 'schema' is a Firestore database and a 'table' is a top-level
    collection. It has no write methods on purpose: this is a source only."""

    dialect, default_port, system_type, label = "firestore", 443, "FIRESTORE", "Google Firebase (Firestore)"
    transactional_ddl = False

    def __init__(self, spec: ConnectionSpec, connect: Optional[Callable[[], Any]] = None) -> None:
        self.spec = spec
        self._connect = connect or self._open

    # ------------------------------------------------------------------ connection
    @property
    def database_id(self) -> str:
        return str(self.spec.options.get("database_id") or DEFAULT_DATABASE_ID)

    def _open(self) -> Any:
        from google.cloud import firestore
        s = self.spec
        if is_emulator(s):
            from google.auth.credentials import AnonymousCredentials
            with _ENV_LOCK:     # the client reads the variable when it is created and keeps what it found: the variable is put back at once
                before = os.environ.get("FIRESTORE_EMULATOR_HOST")
                os.environ["FIRESTORE_EMULATOR_HOST"] = f"{s.host}:{s.port}"
                try:
                    return firestore.Client(project=s.database or "demo-project", credentials=AnonymousCredentials(), database=self.database_id)
                finally:
                    if before is None:
                        os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
                    else:
                        os.environ["FIRESTORE_EMULATOR_HOST"] = before
        from google.oauth2 import service_account
        info = parse_key(s.password)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        with _ENV_LOCK:         # a real project must never be mistaken for an emulator that another connection left in the environment
            before = os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
            try:
                return firestore.Client(project=s.database or info["project_id"], credentials=credentials, database=self.database_id)
            finally:
                if before is not None:
                    os.environ["FIRESTORE_EMULATOR_HOST"] = before

    def _scrub(self, exc: Any) -> str:
        text = str(exc)
        for secret in (self.spec.password,):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _translate(self, exc: Exception) -> ConnectorError:
        if isinstance(exc, ConnectorError):
            return exc
        try:
            from google.api_core import exceptions as gx
            from google.auth import exceptions as ax
        except ImportError:  # pragma: no cover - installed together with the client
            return ConnectorError(f"Firestore could not answer the request: {self._scrub(exc)[:300]}")
        if isinstance(exc, ValueError):      # the key parsed as JSON but the private key inside it cannot be read: cut off or damaged when it was copied
            return ConnectorAuthError("the private key inside the key file cannot be read (damaged, or cut off when it was copied): load the key file again instead of pasting it")
        if isinstance(exc, (gx.PermissionDenied, gx.Forbidden)):
            return ConnectorAuthError("Firestore refused the request: the service account has no permission on this project (give it the 'Cloud Datastore Viewer' role), "
                                      "or the Cloud Firestore API is not enabled for the project")
        if isinstance(exc, (gx.Unauthenticated, ax.GoogleAuthError)):
            return ConnectorAuthError("Google rejected the service-account key (revoked, deleted, expired or the computer's clock is far off); create a new key")
        if isinstance(exc, gx.NotFound):
            return ConnectorError(f"project '{self.spec.database}' or its Firestore database '{self.database_id}' was not found: check the project ID and the database ID "
                                  f"(Firestore must be created in the project, in Native mode)")
        if isinstance(exc, (gx.DeadlineExceeded, gx.ServiceUnavailable, gx.RetryError)):
            return ConnectorError("could not reach Firestore in time: check the internet connection, firewall and proxy (it talks to firestore.googleapis.com on port 443)"
                                  if not is_emulator(self.spec) else f"could not reach the Firestore emulator at {self.spec.host}:{self.spec.port}")
        if isinstance(exc, gx.ResourceExhausted):
            return ConnectorError("Firestore quota exhausted. The free (Spark) plan allows 20,000 writes, 20,000 deletes and 50,000 reads a day, and the day starts again at midnight "
                                  "Pacific time. Wait, deliver a smaller part, or move the project to the paid (Blaze) plan. A delivery that ran out of quota has been removed again.")
        return ConnectorError(f"Firestore could not answer the request: {self._scrub(exc)[:300]}")

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
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing is a courtesy
                pass

    # ------------------------------------------------------------------ browsing
    def test(self) -> dict[str, Any]:
        started = time.monotonic()

        def go(client: Any) -> str:
            for _ in client.collections(timeout=self.spec.connect_timeout):    # asks the service, so a bad key / project / permission fails here
                break
            return "Cloud Firestore (emulator)" if is_emulator(self.spec) else "Cloud Firestore"
        version = self._run(go)
        return {"ok": True, "server_version": version, "latency_ms": int((time.monotonic() - started) * 1000)}

    def schemas(self, include_system: bool = False) -> list[str]:
        return [self.database_id]

    def _collection_names(self, client: Any) -> list[str]:
        return sorted(c.id for c in client.collections(timeout=60))

    def _check(self, client: Any, schema: str, table: str) -> Any:
        if schema != self.database_id:
            raise ConnectorError(f"this connection reads the Firestore database '{self.database_id}', not '{schema}'")
        if table not in self._collection_names(client):
            raise ConnectorError(f"collection {table} was not found in database '{schema}', or this account cannot see it (only top-level collections are read)")
        return client.collection(table)

    @staticmethod
    def _count(coll: Any) -> int:
        result = coll.count().get(timeout=120)
        return int(result[0][0].value)

    def tables(self, schema: str, include_views: bool = True, name_filter: str = "", limit: int = 2000) -> list[TableInfo]:
        def go(client: Any) -> list[TableInfo]:
            needle = name_filter.strip().lower()
            out = []
            for name in self._collection_names(client):
                if needle and needle not in name.lower():
                    continue
                try:
                    estimate: Optional[int] = self._count(client.collection(name)) if len(out) < 50 else None   # a count is a (cheap) billed read: not for hundreds of collections
                except Exception:  # noqa: BLE001
                    estimate = None
                out.append(TableInfo(schema, name, "TABLE", estimate))
            return out[:limit]
        return self._run(go)

    def _sample_columns(self, coll: Any, table: str) -> list[ColumnMeta]:
        types: dict[str, set[str]] = {}
        for snap in coll.order_by("__name__").limit(SAMPLE_DOCUMENTS).stream(timeout=120):
            for name, kind in _type_names(_plain(snap.to_dict() or {})).items():
                types.setdefault(name, set()).add(kind)
        if not types:
            return []
        names = ["_id"] + [n for n in types if n != "_id"]
        if len(names) > MAX_COLUMNS:
            raise ConnectorError(f"{table} has more than {MAX_COLUMNS} distinct field names in its first {SAMPLE_DOCUMENTS} documents; it is not table-shaped")
        return [ColumnMeta(name=n, data_type="|".join(sorted(types.get(n, {"string"}))), length=None, nullable=n != "_id", is_primary_key=n == "_id", position=i + 1)
                for i, n in enumerate(names)]

    def columns(self, schema: str, table: str) -> list[ColumnMeta]:
        """From a sample (the first documents by ID): the extract reads every document and may find more fields."""
        def go(client: Any) -> list[ColumnMeta]:
            cols = self._sample_columns(self._check(client, schema, table), table)
            if not cols:
                raise ConnectorError(f"{schema}.{table} has no documents")
            return cols
        return self._run(go)

    def count(self, schema: str, table: str) -> int:
        return self._run(lambda c: self._count(self._check(c, schema, table)))

    def preview(self, schema: str, table: str, limit: int = 20) -> tuple[list[ColumnMeta], list[list[str]]]:
        limit = max(1, min(int(limit), 200))

        def go(client: Any) -> tuple[list[ColumnMeta], list[list[str]]]:
            coll = self._check(client, schema, table)
            cols = self._sample_columns(coll, table)
            rows = [flatten_document(s) for s in coll.order_by("__name__").limit(limit).stream(timeout=120)]
            return cols, [[r.get(c.name, "") for c in cols] for r in rows]
        return self._run(go)

    # ------------------------------------------------------------------ extraction
    def _documents(self, coll: Any):
        """Every document of the collection in document-ID order, a page at a time (one long stream would be cut off by the service on a large collection)."""
        last = None
        while True:
            query = coll.order_by("__name__").limit(BATCH)
            if last is not None:
                query = query.start_after(last)
            page = list(query.stream(timeout=300))
            if not page:
                return
            yield from page
            last = page[-1]

    def extract_csv(self, schema: str, table: str, columns: Optional[list[str]], out_path: Path, max_bytes: int,
                    progress: Optional[Callable[[int], None]] = None) -> ExtractResult:
        started = time.monotonic()

        def go(client: Any) -> ExtractResult:
            coll = self._check(client, schema, table)
            source_count = self._count(coll)
            count, names = rows_to_csv((flatten_document(s) for s in self._documents(coll)), columns, out_path, max_bytes, f"{schema}.{table}", progress)
            return ExtractResult(path=out_path, rows=count, source_count=source_count, bytes_written=out_path.stat().st_size, columns=names,
                                 seconds=time.monotonic() - started)
        return self._run(go)

    # ------------------------------------------------------------------ delivering into Firestore (a target connection)
    @staticmethod
    def natural_case(name: str) -> str:
        return name                                            # collection and field names keep exactly the case they were given

    @staticmethod
    def check_new_name(name: str, kind: str = "table") -> str:
        word = "collection" if kind == "table" else "field"
        if not NEW_NAME.match(name or ""):
            raise ConnectorError(f"'{name}' cannot be a Firestore {word} name here: use letters, digits and '_', starting with a letter (at most 100 characters)")
        return name

    @staticmethod
    def _type_sql(col: ColumnDef) -> str:
        return "integer" if col.kind == "int" else "string"

    def create_table_sql(self, schema: str, table: str, cols: Sequence[ColumnDef], primary_key: Sequence[str]) -> str:
        """What a delivery would create, in words (Firestore has no DDL)."""
        lines = [f"Firestore database {schema}, new collection {table}: one document per record.",
                 f"Document ID = {' + '.join(primary_key)} (a '/' in a key is written as %2F). Fields:"]
        lines += [f"  {c.name}: {self._type_sql(c)}" + ("" if c.nullable else " (always set)") for c in cols]
        return "\n".join(lines)

    @staticmethod
    def doc_id(key: Any) -> str:
        """A record's key as a Firestore document ID (no '/', not '.' or '..', not a reserved __name__, at most 1500 bytes)."""
        from src.connectors.base import to_text
        text = to_text(key).replace("%", "%25").replace("/", "%2F")
        if not text.strip():
            raise ConnectorError("a record has an empty key: it cannot become a document")
        if text in (".", "..") or (text.startswith("__") and text.endswith("__")):
            text = "%" + format(ord(text[0]), "02X") + text[1:]
        if len(text.encode("utf-8")) > 1500:
            raise ConnectorError(f"the key {text[:40]}... is longer than the 1,500 bytes a document ID may have")
        return text

    def _check_database(self, schema: str) -> None:
        if schema != self.database_id:
            raise ConnectorError(f"this connection writes to the Firestore database '{self.database_id}', not '{schema}'")

    @staticmethod
    def _value(col: ColumnDef, value: Any) -> Any:
        if value is None:
            return None
        return int(value) if col.kind == "int" else str(value)

    @staticmethod
    def _delete_refs(client: Any, refs: Iterable[Any]) -> int:
        """Delete documents a batch at a time; -> how many."""
        done, batch, n = 0, client.batch(), 0
        for ref in refs:
            batch.delete(ref)
            n += 1
            if n == WRITE_BATCH:
                batch.commit()
                done += n
                batch, n = client.batch(), 0
        if n:
            batch.commit()
            done += n
        return done

    def _fingerprint(self, client: Any, table: str, names: Sequence[str], key_positions: Sequence[int]) -> Fingerprint:
        fp = Fingerprint(list(names), list(key_positions))
        for snap in self._documents(client.collection(table)):
            data = snap.to_dict() or {}
            fp.add([data.get(n) for n in names])
        return fp

    def fingerprint_table(self, schema: str, table: str, columns: list[str], key_columns: list[str]) -> Fingerprint:
        """The content fingerprint of what the collection holds NOW (the delivery's own check, repeated at any later time by `verify`)."""
        def go(client: Any) -> Fingerprint:
            self._check_database(schema)
            if table not in self._collection_names(client):
                raise ConnectorError(f"collection {table} was not found in database '{schema}'")
            return self._fingerprint(client, table, columns, [columns.index(k) for k in key_columns])
        return self._run(go)

    def load_table(self, schema: str, table: str, cols: list[ColumnDef], primary_key: list[str], rows: Iterable[Sequence[Any]], mode: str = "create", batch_size: int = 1000,
                   expected_rows: Optional[int] = None, progress: Optional[Callable[[int], None]] = None, verify_values: bool = True,
                   owned_marker_columns: Sequence[str] = ()) -> LoadOutcome:
        """Write the rows as documents of a NEW collection; the count and (unless switched off) every value are read back and compared with what was sent, and if anything is
        wrong the documents written are deleted again, so nothing is kept."""
        if mode != "create":
            raise ConnectorError(NO_REPLACE if mode == "replace" else "mode must be 'create'")
        table = self.check_new_name(table)
        for c in cols:
            self.check_new_name(c.name, "column")
        names = [c.name for c in cols]
        if len(set(names)) != len(names):
            raise ConnectorError("two columns would have the same name: " + ", ".join(sorted({n for n in names if names.count(n) > 1})))
        if len(primary_key) != 1 or primary_key[0] not in names:
            raise ConnectorError("a Firestore delivery needs exactly one key column (it becomes the document ID)")
        key_pos = names.index(primary_key[0])
        started = time.monotonic()
        ddl = self.create_table_sql(schema, table, cols, primary_key)

        def go(client: Any) -> LoadOutcome:
            self._check_database(schema)
            coll = client.collection(table)
            if any(True for _ in coll.limit(1).stream(timeout=60)):
                raise ConnectorError(f"collection {table} already exists and holds documents; deliver into a new collection name (a delivery never adds to or overwrites existing documents)")
            sent_fp = Fingerprint(names, [key_pos])
            written: list[Any] = []
            seen: set[str] = set()
            try:
                batch, n = client.batch(), 0
                for row in rows:
                    row = tuple(row)
                    ref = coll.document(self.doc_id(row[key_pos]))
                    if ref.id in seen:
                        raise ConnectorError(f"two records have the key {row[key_pos]!r}: a key can arrive only once")
                    seen.add(ref.id)
                    sent_fp.add(row)
                    batch.set(ref, {c.name: self._value(c, v) for c, v in zip(cols, row)})
                    written.append(ref)
                    n += 1
                    if n == min(WRITE_BATCH, max(1, batch_size)):
                        batch.commit()
                        batch, n = client.batch(), 0
                        if progress:
                            progress(len(written))
                if n:
                    batch.commit()
                sent = len(written)
                in_target = self._count(coll)
                if in_target != sent or (expected_rows is not None and sent != expected_rows):
                    raise ConnectorError(f"row count check failed: {sent} record(s) were sent, the collection holds {in_target}"
                                         + (f", {expected_rows} were expected" if expected_rows is not None else "") + "; nothing was kept")
                if verify_values:
                    held = self._fingerprint(client, table, names, [key_pos])
                    wrong = sent_fp.differing_columns(held)
                    if wrong:
                        raise ConnectorError(f"value check failed: Firestore holds different values than were sent in field(s) {', '.join(wrong)} ({held.rows} document(s) read back); nothing was kept")
                return LoadOutcome(ddl=ddl, rows_inserted=sent, rows_in_target=in_target, replaced_existing=False, seconds=time.monotonic() - started,
                                   values_verified=verify_values, cells_compared=sent_fp.cells if verify_values else 0, value_digest=sent_fp.digest if verify_values else "")
            except BaseException as exc:
                try:
                    removed = self._delete_refs(client, written)           # compensate: leave nothing behind
                except Exception:  # noqa: BLE001
                    raise ConnectorError(f"the delivery failed ({self._scrub(exc)[:200]}) AND the documents already written could not be removed: collection {table} in database "
                                         f"'{schema}' now holds a partial delivery; delete that collection in the Firebase console before delivering again") from None
                if isinstance(exc, Exception) and type(exc).__module__.split(".")[0] in ("google", "grpc"):
                    raise self._translate(exc) from None         # an answer from Google
                if isinstance(exc, Exception) and not isinstance(exc, ConnectorError):
                    wrapped = _RowsFailed(str(exc))                # not Firestore's to explain (the source of the rows failing, say): handed back unchanged below
                    wrapped.original = exc
                    raise wrapped from None
                raise
        try:
            return self._run(go)
        except _RowsFailed as failed:
            raise failed.original from None

    def rollback_delivery(self, schema: str, table: str, run_column: str, run_id: int, expected_rows: Optional[int] = None) -> RollbackOutcome:
        """Delete exactly the documents that carry `run_id` in `run_column`; refuse when their number is not what was delivered. Firestore has no empty collection, so when nothing is
        left the collection is gone too."""
        started = time.monotonic()

        def go(client: Any) -> RollbackOutcome:
            from google.cloud.firestore_v1.base_query import FieldFilter
            self._check_database(schema)
            coll = client.collection(table)
            if table not in self._collection_names(client):
                raise ConnectorError(f"collection {table} was not found in database '{schema}': nothing to roll back")
            total = self._count(coll)
            mine_query = coll.where(filter=FieldFilter(run_column, "==", int(run_id)))
            mine = int(mine_query.count().get(timeout=120)[0][0].value)
            if mine == 0:
                raise ConnectorError(f"{schema}.{table} holds no documents of run {run_id}: nothing to roll back")
            if expected_rows is not None and mine != expected_rows:
                raise ConnectorError(f"{schema}.{table} holds {mine} document(s) of run {run_id} but {expected_rows} were delivered; it has been changed since, so it is not rolled back automatically")
            removed = self._delete_refs(client, (snap.reference for snap in mine_query.stream(timeout=300)))
            left = self._count(coll)
            gone = int(mine_query.count().get(timeout=120)[0][0].value)
            if gone != 0 or left != total - mine:
                raise ConnectorError(f"the rollback check failed (before {total}, run documents {mine}, removed {removed}, after {left}, run documents still there {gone}); "
                                     "look at the collection in the Firebase console")
            return RollbackOutcome(removed=mine, remaining=left, table_dropped=left == 0, seconds=time.monotonic() - started)
        return self._run(go)

    def probe_write(self, schema: str) -> str:
        """Prove this account can write, read and delete a document in the database (a scratch document in a scratch collection, always removed)."""
        name = "erp_probe_" + uuid.uuid4().hex[:8]

        def go(client: Any) -> str:
            self._check_database(schema)
            ref = client.collection(name).document("probe")
            try:
                ref.set({"PROBE_ID": 1, "PROBE_TEXT": "ok"}, timeout=60)
                snap = ref.get(timeout=60)
                if not snap.exists or (snap.to_dict() or {}).get("PROBE_TEXT") != "ok":
                    raise ConnectorError("the probe document could not be read back")
            finally:
                try:
                    ref.delete()
                except Exception:  # noqa: BLE001
                    pass
            return f"write, read back and delete of one test document all succeeded in Firestore database {schema}"
        return self._run(go)

    def destination_checks(self, *, rows: int, key_stats: Optional[dict] = None, table: Optional[str] = None, exact: bool = False, check_target: bool = False) -> list[dict[str, str]]:
        """What Firebase has to say about delivering these records: the cost, the free plan's quota, keys that cannot become document IDs, a collection that is not new."""
        from src.services.destination_checks import check
        out: list[dict[str, str]] = []
        upto = "" if exact else "up to "
        out.append(check("info", "DEST_COST", f"Firebase will receive {upto}{rows:,} document(s): one write each, plus about as many reads to verify them (and the same again for every later 'verify')."))
        if rows > FREE_PLAN_WRITES:
            out.append(check("warn", "DEST_QUOTA", f"{upto}{rows:,} writes do not fit the free (Spark) plan, which allows {FREE_PLAN_WRITES:,} writes and 50,000 reads a day. The application cannot see which "
                             "plan the project is on.", "Move the Firebase project to the paid (Blaze) plan, or deliver a smaller part (rows to leave out) and the rest on another day."))
        stats = key_stats or {}
        if stats.get("empty"):
            out.append(check("block", "DEST_KEY_EMPTY", f"{stats['empty']:,} record(s) have an empty key. In Firebase the key becomes the document ID, which cannot be empty.",
                             "Choose a primary key that is always filled, or leave those rows out (Rows to leave out)."))
        if stats.get("too_long"):
            out.append(check("block", "DEST_KEY_TOO_LONG", f"{stats['too_long']:,} record(s) have a key longer than 1,500 bytes, the limit of a document ID.", "Use a shorter key column."))
        if stats.get("duplicates"):
            out.append(check("block", "DEST_KEY_DUPLICATE", f"{stats['duplicates']:,} key(s) appear more than once, and each key can become only one document.",
                             "Choose a key that identifies one record, or merge the duplicates first."))
        if stats.get("with_slash"):
            out.append(check("info", "DEST_KEY_SLASH", f"{stats['with_slash']:,} key(s) contain '/', which a document ID cannot: it is written as %2F (the key itself is kept in the field LEGACY_KEY)."))
        if table:
            try:
                self.check_new_name(table)
            except ConnectorError as exc:
                out.append(check("block", "DEST_NAME", str(exc), "Choose another collection name."))
            else:
                if check_target:
                    try:
                        exists = bool(self._run(lambda client: any(True for _ in client.collection(table).limit(1).stream(timeout=60))))
                    except ConnectorError as exc:
                        out.append(check("warn", "DEST_UNREACHABLE", f"could not look at the destination: {exc}", "Test the target connection again (the key is kept in memory only and is lost when the server restarts)."))
                    else:
                        if exists:
                            out.append(check("block", "DEST_COLLECTION_EXISTS", f"collection {table} already holds documents, and a delivery only goes into a new collection.",
                                             "Choose another collection name, or roll the earlier delivery back first."))
        return out

    def merge_table(self, *args: Any, **kwargs: Any) -> MergeOutcome:
        raise ConnectorError(NO_MERGE)

    def rollback_merge(self, *args: Any, **kwargs: Any) -> MergeRollbackOutcome:
        raise ConnectorError(NO_MERGE)
