"""Deliver a validated run into a DATABASE target (Oracle, SQL Server, PostgreSQL, MySQL).

This is a delivery, not the SAP load: it never touches the run's phase or counters, so a run can be rehearsed into a database and
still be loaded into SAP afterwards. The caller (the API) enforces the same gate as every load: steward sign-off recorded and the
governance gate passing right now. This service then:

  * plans the target table from the validated payload: one column per SAP target field (BUT000_NAME_ORG1, ADRC_CITY1, ...) with
    the length the SAP catalog gives it, plus LEGACY_KEY (the source key, the primary key of the new table, so a record can never
    arrive twice) and MIG_RUN_ID; optionally the raw legacy values (SRC_*)
  * creates the table and inserts the rows in ONE all-or-nothing operation, reading the row count back before committing, and then
    every VALUE (a per-column content fingerprint of what was sent against what the database holds), so a delivery that arrives with
    the right number of rows but altered values is refused; `verify` repeats that comparison against the live table at any later time
  * writes the evidence to the run folder (db_loads.json) for the reconciliation certificate
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.base import ColumnDef, Connector, ConnectorError
from src.connectors.fingerprint import Fingerprint
from src.db.enums import TargetType
from src.db.models import CatColumn, CatDataSource, CatTable
from src.services._duckdb_util import bounded_connection, lit, q

DELIVERIES_FILE = "db_loads.json"
KEY_COLUMN, RUN_COLUMN = "LEGACY_KEY", "MIG_RUN_ID"


class DbLoadError(Exception):
    """The payload cannot be delivered (nothing usable in it, or the target refused it); the message is safe to show."""


class DbLoaderService:
    def __init__(self, session_factory: sessionmaker[Session], memory_limit_mb: int = 512) -> None:
        self._sf = session_factory
        self._memory = memory_limit_mb

    # ------------------------------------------------------------------ planning
    def _target_lengths(self) -> dict[str, Optional[int]]:
        """{'BUT000_NAME_ORG1': 40, ...} from the SAP target catalog."""
        with self._sf() as s:
            rows = s.execute(
                select(CatTable.table_name, CatColumn.column_name, CatColumn.char_max_length)
                .join(CatColumn, CatColumn.table_id == CatTable.id).join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == "BUSINESS_PARTNER")).all()
        return {f"{t}_{c}": length for t, c, length in rows}

    def target_columns(self, valid_path: str | Path) -> list[str]:
        """The SAP target fields (BUT000_NAME_ORG1, ADRC_CITY1, ...) a validated payload holds, sorted: what a delivery or a delta compares."""
        catalog = self._target_lengths()
        with bounded_connection(self._memory) as con:
            names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(Path(valid_path).as_posix())})").fetchall()]
        return sorted(n for n in names if n in catalog)

    def plan(self, valid_path: str | Path, run_id: int, include_source_columns: bool = False) -> tuple[list[ColumnDef], str, int]:
        """(columns of the new table, the SELECT that yields their values in order, row count)."""
        path = Path(valid_path)
        if not path.is_file():
            raise DbLoadError("the run has no validated payload file")
        catalog = self._target_lengths()
        with bounded_connection(self._memory) as con:
            names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(path.as_posix())})").fetchall()]
            total = int(con.execute(f"SELECT count(*) FROM read_parquet({lit(path.as_posix())})").fetchone()[0])
            targets = [n for n in names if n in catalog]
            sources = [n for n in names if n.startswith("SRC_")] if include_source_columns else []
            widths = {}
            if sources:
                row = con.execute("SELECT " + ", ".join(f"coalesce(max(length(CAST({q(n)} AS VARCHAR))), 1)" for n in sources)
                                  + f" FROM read_parquet({lit(path.as_posix())})").fetchone()
                widths = dict(zip(sources, row))
        if "_SRC_PK" not in names:
            raise DbLoadError("the payload has no _SRC_PK column")
        if not targets:
            raise DbLoadError("the payload holds no SAP target fields (BUT000_*, ADRC_*, KNB1_*, KNVV_*) to deliver")
        cols = [ColumnDef(KEY_COLUMN, "text", 255, nullable=False), ColumnDef(RUN_COLUMN, "int", nullable=False)]
        cols += [ColumnDef(n, "text", int(catalog[n] or 255)) for n in targets]
        taken = {c.name.lower() for c in cols}
        for n in sources:                                    # real legacy names are 'Customer Name', 'Tax-ID', 'CUSTOMER_ADDRESS_LINE_1_LONG', or differ only by case
            safe = self.safe_column_name(n, taken)
            taken.add(safe.lower())
            cols.append(ColumnDef(safe, "text", max(1, int(widths[n]))))
        select_sql = (f'SELECT CAST("_SRC_PK" AS VARCHAR), {int(run_id)}, ' + ", ".join(f"CAST({q(n)} AS VARCHAR)" for n in targets + sources)
                      + f" FROM read_parquet({lit(path.as_posix())}) ORDER BY 1")
        return cols, select_sql, total

    @staticmethod
    def safe_column_name(name: str, taken: set[str]) -> str:
        """SRC_<legacy name> reduced to letters, digits and '_', at most 30 characters, unique among `taken` (case-insensitively)."""
        base = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", name)).strip("_") or "COL"
        candidate = base[:30]
        n = 2
        while candidate.lower() in taken:
            suffix = f"_{n}"
            candidate = base[:30 - len(suffix)] + suffix
            n += 1
        return candidate

    def preview(self, connector: Connector, valid_path: str | Path, run_id: int, schema: str, table: str, include_source_columns: bool = False) -> dict[str, Any]:
        cols, _, total = self.plan(valid_path, run_id, include_source_columns)
        name = connector.check_new_name(table)
        cols = [ColumnDef(connector.natural_case(c.name), c.kind, c.length, c.nullable) for c in cols]
        return {"table": name, "rows": total, "columns": [{"name": c.name, "type": connector._type_sql(c), "nullable": c.nullable} for c in cols],
                "ddl": connector.create_table_sql(schema, name, cols, [connector.natural_case(KEY_COLUMN)])}

    # ------------------------------------------------------------------ delivery
    def _rows(self, select_sql: str) -> Iterator[tuple]:
        with bounded_connection(self._memory) as con:
            cur = con.execute(select_sql)
            while True:
                batch = cur.fetchmany(2000)
                if not batch:
                    return
                for row in batch:
                    yield (row[0], int(row[1]), *row[2:])

    def deliver(self, connector: Connector, run_id: int, valid_path: str | Path, run_dir: Path, connection_name: str, schema: str, table: str,
                mode: str, user: str, include_source_columns: bool = False, batch_size: int = 1000, verify_values: bool = True) -> dict[str, Any]:
        cols, select_sql, total = self.plan(valid_path, run_id, include_source_columns)
        if total == 0:
            raise DbLoadError("the validated payload has no rows to deliver")
        started = datetime.now(timezone.utc)
        rows = self._rows(select_sql)
        try:
            outcome = connector.load_table(schema, table, cols, [KEY_COLUMN], rows, mode=mode, batch_size=batch_size, expected_rows=total,
                                           verify_values=verify_values)
        except ConnectorError as exc:
            raise DbLoadError(str(exc)) from exc
        finally:
            rows.close()                                         # releases the DuckDB connection and its temp folder now, even when the load failed halfway
        record = {
            "run_id": run_id, "connection": connection_name, "dialect": connector.dialect, "host": connector.spec.host, "port": connector.spec.port,
            "database": connector.spec.database, "schema": schema, "table": connector.natural_case(table), "mode": mode,
            "columns": len(cols), "include_source_columns": include_source_columns, "valid_records": total, "rows_loaded": outcome.rows_inserted,
            "rows_in_target": outcome.rows_in_target, "replaced_existing": outcome.replaced_existing, "seconds": round(outcome.seconds, 2),
            "loaded_by": user, "loaded_at": started.isoformat(timespec="seconds"), "ddl": outcome.ddl,
            "values_verified": outcome.values_verified, "cells_compared": outcome.cells_compared, "value_digest": outcome.value_digest,
            "verifications": [],
        }
        history = self.deliveries(run_dir)
        history.append(record)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / DELIVERIES_FILE).write_text(json.dumps(history, indent=1), encoding="utf-8")
        return record

    def verify(self, connector: Connector, run_id: int, valid_path: str | Path, run_dir: Path, connection_name: str, schema: str, table: str,
               user: str) -> dict[str, Any]:
        """Compare what the target table holds NOW with the run's validated payload, value by value. Catches a table that was edited,
        truncated or partly deleted after the delivery. The result is appended to that delivery's record (the certificate reads it)."""
        history = self.deliveries(run_dir)
        table = connector.natural_case(table)
        index = next((i for i in range(len(history) - 1, -1, -1)
                      if (history[i]["connection"], history[i]["schema"], history[i]["table"]) == (connection_name, schema, table)), None)
        if index is None:
            raise DbLoadError(f"run {run_id} has no recorded delivery into {schema}.{table} on connection '{connection_name}'")
        delivery = history[index]
        cols, select_sql, total = self.plan(valid_path, run_id, bool(delivery.get("include_source_columns")))
        names = [connector.natural_case(c.name) for c in cols]
        key = connector.natural_case(KEY_COLUMN)
        expected = Fingerprint(names, [names.index(key)])
        rows = self._rows(select_sql)
        try:
            for row in rows:
                expected.add(row)
        finally:
            rows.close()
        try:
            held = connector.fingerprint_table(schema, table, names, [key])
        except ConnectorError as exc:
            raise DbLoadError(str(exc)) from exc
        differing = expected.differing_columns(held)
        result = {
            "verified": not differing and held.rows == expected.rows, "differing_columns": differing, "rows_in_payload": expected.rows,
            "rows_in_target": held.rows, "cells_compared": expected.cells, "digest": held.digest, "checked_by": user,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        history[index].setdefault("verifications", []).append(result)
        (run_dir / DELIVERIES_FILE).write_text(json.dumps(history, indent=1), encoding="utf-8")
        return {"run_id": run_id, "connection": connection_name, "schema": schema, "table": table, **result}

    def rollback(self, connector: Connector, run_id: int, run_dir: Path, connection_name: str, schema: str, table: str, user: str, reason: str) -> dict[str, Any]:
        """Undo a delivery into a database target: delete the run's rows from the table (and drop the table if that empties it). Needs a
        reason; recorded with who and when, and shown on the certificate. A delivery that replaced an older table cannot bring the old
        table back: its rows are gone, so say so before rolling back."""
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise DbLoadError("give a reason for the rollback (it is kept in the record)")
        history = self.deliveries(run_dir)
        table = connector.natural_case(table)
        index = next((i for i in range(len(history) - 1, -1, -1)
                      if (history[i]["connection"], history[i]["schema"], history[i]["table"]) == (connection_name, schema, table)), None)
        if index is None:
            raise DbLoadError(f"run {run_id} has no recorded delivery into {schema}.{table} on connection '{connection_name}'")
        delivery = history[index]
        if delivery.get("rolled_back"):
            raise DbLoadError(f"this delivery was already rolled back by {delivery['rolled_back']['by']} at {delivery['rolled_back']['at']}")
        if delivery.get("mode") == "merge":
            restored = self._rollback_merge(connector, run_id, run_dir, delivery, schema, table)
            delivery["rolled_back"] = {"by": user, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason,
                                       "replaced_an_older_table": False, **restored}
            (run_dir / DELIVERIES_FILE).write_text(json.dumps(history, indent=1), encoding="utf-8")
            return {"run_id": run_id, "connection": connection_name, "schema": schema, "table": table, **delivery["rolled_back"]}
        try:
            outcome = connector.rollback_delivery(schema, table, connector.natural_case(RUN_COLUMN), run_id, expected_rows=delivery["rows_loaded"])
        except ConnectorError as exc:
            raise DbLoadError(str(exc)) from exc
        delivery["rolled_back"] = {"by": user, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason, "rows_removed": outcome.removed,
                                   "rows_remaining": outcome.remaining, "table_dropped": outcome.table_dropped,
                                   "replaced_an_older_table": bool(delivery.get("replaced_existing"))}
        (run_dir / DELIVERIES_FILE).write_text(json.dumps(history, indent=1), encoding="utf-8")
        return {"run_id": run_id, "connection": connection_name, "schema": schema, "table": table, **delivery["rolled_back"]}

    # ------------------------------------------------------------------ delta (merge) deliveries
    @staticmethod
    def before_image_path(run_dir: Path, connection_name: str, schema: str, table: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{connection_name}_{schema}_{table}")
        return run_dir / f"merge_before_{slug}.csv.gz"

    @staticmethod
    def _keys(path: Optional[str]) -> Iterator[str]:
        if not path or not Path(path).is_file():
            return
        with bounded_connection(256) as con:
            cur = con.execute(f'SELECT CAST("_SRC_PK" AS VARCHAR) FROM read_parquet({lit(Path(path).as_posix())}) ORDER BY 1')
            while True:
                batch = cur.fetchmany(2000)
                if not batch:
                    return
                for (k,) in batch:
                    yield k

    def deliver_merge(self, connector: Connector, run_id: int, valid_path: str | Path, delta_path: Optional[str], removed_path: Optional[str], baseline_run: int,
                      run_dir: Path, connection_name: str, schema: str, table: str, user: str, include_source_columns: bool = False,
                      delete_removed: bool = False, batch_size: int = 300, verify_values: bool = True) -> dict[str, Any]:
        """Apply a run's delta to a table an earlier run delivered: new and changed records are written (a changed record replaces its row),
        and with `delete_removed` the records that disappeared are deleted. One transaction; every replaced or deleted row is first copied to a
        before-image in the run folder, so `rollback` can put the table back exactly as it was."""
        cols, _, _ = self.plan(valid_path, run_id, include_source_columns)             # the columns of the table, from the full payload
        n_delta = 0
        rows: Iterator[tuple] = iter(())
        if delta_path:
            _, select_sql, n_delta = self.plan(delta_path, run_id, include_source_columns)
            rows = self._rows(select_sql)
        removing = delete_removed and bool(removed_path)
        if not n_delta and not removing:
            raise DbLoadError("the delta holds nothing to deliver: no new or changed records" + (" and nothing removed" if delete_removed else ""))
        run_dir.mkdir(parents=True, exist_ok=True)
        image = self.before_image_path(run_dir, connection_name, schema, table)
        started = datetime.now(timezone.utc)
        handle = gzip.open(image, "wt", encoding="utf-8", newline="")
        writer = csv.writer(handle, lineterminator="\n")
        try:
            outcome = connector.merge_table(schema, table, cols, KEY_COLUMN, rows, delete_keys=self._keys(removed_path) if removing else (),
                                            batch_size=batch_size, before_sink=writer.writerows, verify_values=verify_values, expected_rows=n_delta)
            handle.close()
        except ConnectorError as exc:
            handle.close()
            image.unlink(missing_ok=True)
            raise DbLoadError(str(exc)) from exc
        except Exception:
            handle.close()
            image.unlink(missing_ok=True)
            raise
        finally:
            if hasattr(rows, "close"):
                rows.close()
        record = {
            "run_id": run_id, "connection": connection_name, "dialect": connector.dialect, "host": connector.spec.host, "port": connector.spec.port,
            "database": connector.spec.database, "schema": schema, "table": connector.natural_case(table), "mode": "merge", "baseline_run": baseline_run,
            "columns": len(cols), "include_source_columns": include_source_columns, "valid_records": n_delta, "rows_loaded": outcome.inserted + outcome.updated,
            "inserted_new": outcome.inserted, "updated": outcome.updated, "deleted": outcome.deleted, "rows_before": outcome.rows_before,
            "rows_in_target": outcome.rows_after, "replaced_existing": False, "seconds": round(outcome.seconds, 2), "loaded_by": user,
            "loaded_at": started.isoformat(timespec="seconds"), "ddl": "", "values_verified": outcome.values_verified, "cells_compared": outcome.cells_compared,
            "value_digest": outcome.value_digest, "verifications": [], "before_image": image.name, "before_rows": outcome.before_rows,
        }
        history = self.deliveries(run_dir)
        history.append(record)
        (run_dir / DELIVERIES_FILE).write_text(json.dumps(history, indent=1), encoding="utf-8")
        return record

    def _rollback_merge(self, connector: Connector, run_id: int, run_dir: Path, delivery: dict[str, Any], schema: str, table: str) -> dict[str, Any]:
        image = run_dir / delivery["before_image"]
        if not image.is_file():
            raise DbLoadError(f"the before-image of this merge ({delivery['before_image']}) is missing, so the table cannot be restored automatically")
        cols, _, _ = self.plan(self._delivered_payload(run_dir, run_id), run_id, bool(delivery.get("include_source_columns")))

        def saved() -> Iterator[list[str]]:
            with gzip.open(image, "rt", encoding="utf-8", newline="") as fh:
                yield from csv.reader(fh)

        try:
            out = connector.rollback_merge(schema, table, cols, KEY_COLUMN, RUN_COLUMN, run_id, saved(), expected_mine=delivery["rows_loaded"],
                                           expected_before=delivery["before_rows"])
        except ConnectorError as exc:
            raise DbLoadError(str(exc)) from exc
        return {"rows_removed": out.removed, "rows_remaining": out.rows_after, "table_dropped": False, "rows_restored": out.restored}

    @staticmethod
    def _delivered_payload(run_dir: Path, run_id: int) -> Path:
        try:
            report = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
            return Path(report["valid_path"])
        except (OSError, ValueError, KeyError) as exc:
            raise DbLoadError(f"run {run_id} has no validated payload to read the table's columns from") from exc

    @staticmethod
    def deliveries(run_dir: Path) -> list[dict[str, Any]]:
        try:
            return json.loads((run_dir / DELIVERIES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
