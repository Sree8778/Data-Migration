"""What the DESTINATION has to say about a delivery, worked out before anybody is asked to approve it or to deliver it.

Each kind of destination has rules of its own: Firebase turns a record's key into a document ID (never empty, at most 1,500 bytes), a database has identifier limits, the free plan
of a cloud service has a daily quota. These used to be discovered at the last step, after approval, the run and the sign-off. A connector that has rules of this kind offers
`destination_checks(...)`; this module works out the facts about the KEYS of the records (the same for every destination that uses the key as an identifier) and asks the connector.

A check is a dict: {level: block | warn | info, code, message, next_step}. `block` stops the delivery (in the dry run: stops the submission), `warn` is shown to the person and the
approver, `info` says what will happen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from src.services._duckdb_util import bounded_connection, lit, q


def key_sql_for(columns: list[str]) -> str:
    """The record key the pipeline will build from these columns: the trimmed parts joined with '|'; a record with a blank part gets a row-number key instead (so NULL here)."""
    parts = ", ".join(f"trim(CAST({q(c)} AS VARCHAR))" for c in columns)
    nulls = " OR ".join(f"{q(c)} IS NULL" for c in columns)
    return f"CASE WHEN {nulls} THEN NULL ELSE concat_ws('|', {parts}) END"


def key_statistics(parquet_path: str | Path, key_sql: str) -> dict[str, int]:
    """Facts about the keys of the records in a file: rows, empty keys, keys too long to be a document ID once '/' is escaped, keys containing '/', repeated keys."""
    with bounded_connection(256) as con:
        row = con.execute(
            "SELECT count(*), count_if(k IS NOT NULL AND trim(k) = ''), count_if(strlen(replace(replace(k, '%', '%25'), '/', '%2F')) > 1500), "
            "count_if(position('/' IN k) > 0), count(k) - count(DISTINCT k) "
            f"FROM (SELECT {key_sql} AS k FROM read_parquet({lit(Path(parquet_path).as_posix())}))").fetchone()
    return {"rows": int(row[0]), "empty": int(row[1]), "too_long": int(row[2]), "with_slash": int(row[3]), "duplicates": int(row[4])}


def checks_for(connector: Any, **facts: Any) -> list[dict[str, str]]:
    """The destination's own checks; none for a connector that has no rules of this kind."""
    fn = getattr(connector, "destination_checks", None)
    return list(fn(**facts)) if fn else []


def check(level: str, code: str, message: str, next_step: str = "") -> dict[str, str]:
    return {"level": level, "code": code, "message": message, "next_step": next_step}


def destination_for_mapping(ctx: Any, mapping_set_id: int, source_table_id: int) -> Optional[dict[str, Any]]:
    """The destination a mapping set was pointed at (None for the default, SAP) with its checks, for the dry run. Needs no password: nothing is connected to."""
    from sqlalchemy import select
    from src.connectors.base import ConnectionSpec
    from src.connectors.dialects import CONNECTORS
    from src.db.models import CatColumn

    name = ctx.mapping_targets.get(mapping_set_id)
    if not name:
        return None
    record = ctx.connections.get(name)
    cls = CONNECTORS.get(record.dialect) if record else None
    if record is None or cls is None:
        return {"connection": name, "label": name, "dialect": "", "checks": [check(
            "warn", "DEST_MISSING", f"the destination connection '{name}' no longer exists", "Choose another destination in the Mapping Studio, or add the connection again.")]}
    label = f"{cls.label} ({name})"
    source = ctx.source_file(source_table_id)
    if source is None or not Path(source).is_file():
        return {"connection": name, "label": label, "dialect": record.dialect, "checks": [check(
            "warn", "DEST_NOT_CHECKED", "no source file is landed for this table, so the destination's rules could not be checked against its keys")]}
    in_file = ctx.file_columns(source_table_id) or set()
    with ctx.session_factory() as s:
        key_columns = [c for c in s.scalars(select(CatColumn.column_name).where(CatColumn.table_id == source_table_id, CatColumn.is_primary_key.is_(True)).order_by(CatColumn.id))
                       if c.lower() in in_file]
    if key_columns:
        stats = key_statistics(source, key_sql_for(key_columns))
    else:                                           # no key: the pipeline numbers the rows, so there is nothing to check about keys
        with bounded_connection(256) as con:
            n = int(con.execute(f"SELECT count(*) FROM read_parquet({lit(Path(source).as_posix())})").fetchone()[0])
        stats = {"rows": n, "empty": 0, "too_long": 0, "with_slash": 0, "duplicates": 0}
    connector = cls(ConnectionSpec(record.dialect, record.host, record.port, record.database, record.user, "", dict(record.options)))
    return {"connection": name, "label": label, "dialect": record.dialect, "checks": checks_for(connector, rows=stats["rows"], key_stats=stats, exact=False)}
