"""Deliver a validated run into a DATABASE target (Oracle, SQL Server, PostgreSQL, MySQL).

This is a delivery, not the SAP load: it never touches the run's phase or counters, so a run can be rehearsed into a database and
still be loaded into SAP afterwards. The caller (the API) enforces the same gate as every load: steward sign-off recorded and the
governance gate passing right now. This service then:

  * plans the target table from the validated payload: one column per SAP target field (BUT000_NAME_ORG1, ADRC_CITY1, ...) with
    the length the SAP catalog gives it, plus LEGACY_KEY (the source key, the primary key of the new table, so a record can never
    arrive twice) and MIG_RUN_ID; optionally the raw legacy values (SRC_*)
  * creates the table and inserts the rows in ONE all-or-nothing operation, reading the row count back before committing
  * writes the evidence to the run folder (db_loads.json) for the reconciliation certificate
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.connectors.base import ColumnDef, Connector, ConnectorError
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
                mode: str, user: str, include_source_columns: bool = False, batch_size: int = 1000) -> dict[str, Any]:
        cols, select_sql, total = self.plan(valid_path, run_id, include_source_columns)
        if total == 0:
            raise DbLoadError("the validated payload has no rows to deliver")
        started = datetime.now(timezone.utc)
        rows = self._rows(select_sql)
        try:
            outcome = connector.load_table(schema, table, cols, [KEY_COLUMN], rows, mode=mode, batch_size=batch_size, expected_rows=total)
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
        }
        history = self.deliveries(run_dir)
        history.append(record)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / DELIVERIES_FILE).write_text(json.dumps(history, indent=1), encoding="utf-8")
        return record

    @staticmethod
    def deliveries(run_dir: Path) -> list[dict[str, Any]]:
        try:
            return json.loads((run_dir / DELIVERIES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
