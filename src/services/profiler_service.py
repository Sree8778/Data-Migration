"""Deterministic, LLM-free data profiler built on DuckDB (Module 2).

Profiles CSV / Parquet staging files in a bounded in-memory DuckDB (default 1 GB) and
persists the outcome into the Module 1 catalog (`cat_columns`, `cat_tables`).

Conventions
* Every value is read as text and trimmed; empty / whitespace-only values count as NULL.
  Lengths, patterns and leading zeros therefore describe the data as the legacy system
  delivered it, not as a type-inferring parser would reinterpret it.
* `detected_type` comes from a separate type-inferring read (CSV) or the file schema (Parquet).
* Same input -> same output: all rankings use explicit tie-breakers, samples are chosen by
  value hash rather than randomly.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import PiiClassification
from src.db.models import CatColumn, CatTable
from src.schemas.profiler import (
    ColumnProfile,
    KeyDuplicateReport,
    PatternConformity,
    SaveProfileSummary,
    TableProfileResult,
    TopValue,
)
from src.services.metadata_service import NotFoundError

PROFILER_VERSION = "1.0.0"

# Full-match patterns applied to every (trimmed, non-null) value.
PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}",
    "iso_country_2": r"[A-Z]{2}",
    "postal_code": r"[A-Za-z0-9][A-Za-z0-9 \-]{2,9}",
    "numeric_string": r"[0-9]+",
    "leading_zero_numeric": r"0[0-9]+",
}

_NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UBIGINT", "UINTEGER",
    "USMALLINT", "UTINYINT", "DECIMAL", "DOUBLE", "FLOAT",
)
_SAMPLE_MAX_CHARS = 100
_MAX_INFERRED_KEYS = 5
_INFERRED_KEY_MIN_UNIQUENESS = 0.95


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _sanitize(value: str) -> str:
    """Strip control characters and cap length so samples are safe to store and display."""
    return "".join(ch if ch.isprintable() else " " for ch in value)[:_SAMPLE_MAX_CHARS]


def _mask_shape(value: str) -> str:
    """Irreversible shape mask used for PII samples: upper -> A, lower -> a, digit -> 9."""
    out = []
    for ch in value:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    return "".join(out)


class ProfilerService:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        memory_limit_mb: int = 1024,
        threads: int = 2,
        type_sample_size: int = 100_000,
    ) -> None:
        self._session_factory = session_factory
        self._memory_limit_mb = memory_limit_mb
        self._threads = threads
        self._type_sample_size = type_sample_size

    # ------------------------------------------------------------------ profiling
    def profile_file(
        self,
        path: str | Path,
        candidate_keys: list[list[str]] | None = None,
    ) -> TableProfileResult:
        """Profile a CSV or Parquet file.

        `candidate_keys`: key column sets to check for duplicates. When omitted, single
        columns that are fully populated and >= 95% unique are inferred as candidate keys.
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        suffix = path.suffix.lower()
        posix = _lit(path.as_posix())
        if suffix in (".csv", ".tsv", ".txt"):
            fmt = "csv"
            raw_text = f"read_csv({posix}, all_varchar=true, header=true, null_padding=true)"
            typed = (
                f"read_csv({posix}, sample_size={self._type_sample_size}, header=true, null_padding=true)"
            )
        elif suffix in (".parquet", ".pq"):
            fmt = "parquet"
            raw_text = typed = f"read_parquet({posix})"
        else:
            raise ValueError(f"unsupported file type '{path.suffix}' (expected CSV or Parquet)")

        with tempfile.TemporaryDirectory(prefix="profiler_") as tmp:
            con = duckdb.connect(":memory:")
            try:
                con.execute(f"SET memory_limit='{self._memory_limit_mb}MB'")
                con.execute(f"SET threads={self._threads}")
                con.execute(f"SET temp_directory={_lit(tmp)}")
                return self._profile(con, path, fmt, raw_text, typed, candidate_keys)
            finally:
                con.close()

    def _profile(self, con, path, fmt, raw_text, typed, candidate_keys) -> TableProfileResult:
        types = [(r[0], r[1]) for r in con.execute(f"DESCRIBE SELECT * FROM {typed}").fetchall()]
        names = [n for n, _ in types]
        if not names:
            raise ValueError("source has no columns")

        # Normalised view: every column as trimmed text, blank -> NULL.
        select_list = ", ".join(
            f"NULLIF(TRIM(CAST({_q(n)} AS VARCHAR)), '') AS {_q(n)}" for n in names
        )
        con.execute(f"CREATE VIEW v AS SELECT {select_list} FROM {raw_text}")

        # One scan for all scalar metrics.
        aggs = ["count(*)"]
        for n in names:
            c = _q(n)
            aggs += [f"count({c})", f"count(DISTINCT {c})", f"min(length({c}))", f"max(length({c}))"]
            aggs += [f"coalesce(count_if(regexp_full_match({c}, {_lit(p)})), 0)" for p in PATTERNS.values()]
        row = con.execute(f"SELECT {', '.join(aggs)} FROM v").fetchone()
        total = int(row[0])
        stride = 4 + len(PATTERNS)

        columns: list[ColumnProfile] = []
        for i, (name, dtype) in enumerate(types):
            base = 1 + i * stride
            non_null, distinct, min_len, max_len = row[base : base + 4]
            matched = row[base + 4 : base + stride]
            conformity = {
                key: PatternConformity(
                    matched=int(m),
                    checked=int(non_null),
                    ratio=round(m / non_null, 4) if non_null else 0.0,
                )
                for key, m in zip(PATTERNS, matched)
            }
            columns.append(
                ColumnProfile(
                    column_name=name,
                    position=i + 1,
                    detected_type=dtype,
                    total_rows=total,
                    null_count=total - int(non_null),
                    null_ratio=round((total - non_null) / total, 4) if total else 0.0,
                    distinct_count=int(distinct),
                    distinct_ratio=round(distinct / non_null, 4) if non_null else 0.0,
                    min_length=None if min_len is None else int(min_len),
                    max_length=None if max_len is None else int(max_len),
                    top_values=self._top_values(con, name),
                    sample_values=self._samples(con, name),
                    conformity=conformity,
                    leading_zero_risk=(
                        dtype.upper().startswith(_NUMERIC_TYPES)
                        and conformity["leading_zero_numeric"].matched > 0
                    ),
                )
            )

        # Full-row duplicates via a 64-bit row hash (collision risk negligible, memory light).
        full_dupes = 0
        if total:
            distinct_rows = con.execute("SELECT count(DISTINCT hash(t)) FROM v t").fetchone()[0]
            full_dupes = total - int(distinct_rows)

        if candidate_keys is None:
            candidate_keys = [
                [c.column_name]
                for c in columns
                if c.null_count == 0 and c.distinct_ratio >= _INFERRED_KEY_MIN_UNIQUENESS
            ][:_MAX_INFERRED_KEYS]
        known = set(names)
        for key in candidate_keys:
            missing = [k for k in key if k not in known]
            if missing:
                raise ValueError(f"candidate key columns not found in source: {missing}")

        return TableProfileResult(
            source_path=str(path),
            file_format=fmt,
            total_rows=total,
            column_count=len(names),
            columns=columns,
            full_row_duplicate_rows=full_dupes,
            full_row_duplicate_ratio=round(full_dupes / total, 4) if total else 0.0,
            key_duplicates=[self._key_duplicates(con, key) for key in candidate_keys],
            profiled_at=datetime.now(timezone.utc),
            profiler_version=PROFILER_VERSION,
        )

    @staticmethod
    def _top_values(con, name: str) -> list[TopValue]:
        c = _q(name)
        rows = con.execute(
            f"SELECT {c}, count(*) AS n FROM v WHERE {c} IS NOT NULL "
            f"GROUP BY {c} ORDER BY n DESC, {c} ASC LIMIT 5"
        ).fetchall()
        return [TopValue(value=v[:_SAMPLE_MAX_CHARS], count=int(n)) for v, n in rows]

    @staticmethod
    def _samples(con, name: str) -> list[str]:
        c = _q(name)
        rows = con.execute(
            f"SELECT DISTINCT {c} FROM v WHERE {c} IS NOT NULL ORDER BY hash({c}), {c} LIMIT 5"
        ).fetchall()
        return [_sanitize(r[0]) for r in rows]

    @staticmethod
    def _key_duplicates(con, key: list[str]) -> KeyDuplicateReport:
        cols = ", ".join(_q(k) for k in key)
        not_null = " AND ".join(f"{_q(k)} IS NOT NULL" for k in key)
        groups, surplus = con.execute(
            f"SELECT count(*), coalesce(sum(n - 1), 0) FROM "
            f"(SELECT count(*) AS n FROM v WHERE {not_null} GROUP BY {cols} HAVING count(*) > 1)"
        ).fetchone()
        samples = con.execute(
            f"SELECT {cols} FROM v WHERE {not_null} GROUP BY {cols} HAVING count(*) > 1 "
            f"ORDER BY count(*) DESC, {cols} LIMIT 5"
        ).fetchall()
        return KeyDuplicateReport(
            columns=list(key),
            duplicate_group_count=int(groups),
            duplicate_row_count=int(surplus),
            sample_duplicate_keys=[[str(x) for x in s] for s in samples],
        )

    # ------------------------------------------------------------------ persistence
    def save_profile_to_catalog(
        self, table_id: int, profile_results: TableProfileResult
    ) -> SaveProfileSummary:
        """Write profiling output into cat_columns / cat_tables in one transaction.

        Columns are matched to the catalog case-insensitively by name; profile columns with
        no catalog counterpart are reported, never auto-created. Samples for columns classified
        RESTRICTED_PII are stored as shape masks, not raw values.

        `table_id` is the integer primary key of cat_tables (Module 1 schema).
        """
        if self._session_factory is None:
            raise RuntimeError("ProfilerService was created without a session_factory")
        with self._session_factory.begin() as session:
            table = session.get(CatTable, table_id)
            if table is None:
                raise NotFoundError(f"table {table_id} does not exist")
            catalog = {
                c.column_name.lower(): c
                for c in session.scalars(select(CatColumn).where(CatColumn.table_id == table_id))
            }
            updated, unmatched, masked = 0, [], []
            seen: set[str] = set()
            for prof in profile_results.columns:
                col = catalog.get(prof.column_name.lower())
                if col is None:
                    unmatched.append(prof.column_name)
                    continue
                seen.add(col.column_name.lower())
                samples = prof.sample_values
                if col.pii_classification == PiiClassification.RESTRICTED_PII:
                    samples = [_mask_shape(s) for s in samples]
                    masked.append(col.column_name)
                col.null_ratio = prof.null_ratio
                col.distinct_ratio = prof.distinct_ratio
                col.sample_values = samples
                updated += 1

            table.row_count_estimate = profile_results.total_rows
            table.last_profiled_at = profile_results.profiled_at
            return SaveProfileSummary(
                table_id=table_id,
                row_count_estimate=profile_results.total_rows,
                columns_updated=updated,
                unmatched_profile_columns=unmatched,
                catalog_columns_not_profiled=sorted(
                    c.column_name for k, c in catalog.items() if k not in seen
                ),
                masked_sample_columns=masked,
            )
