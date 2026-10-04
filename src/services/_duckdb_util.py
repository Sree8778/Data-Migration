"""Shared DuckDB helpers for the Module 4 engines (bounded in-memory connections)."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

DEFAULT_MEMORY_LIMIT_MB = 1536  # 1.5 GB hard cap for every DuckDB instance we start


def q(identifier: str) -> str:
    """Quote an SQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def lit(text: str) -> str:
    """Quote an SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


@contextmanager
def bounded_connection(memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB, threads: int = 2) -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB limited in RAM and threads; spills to a temp dir that is removed on exit."""
    with tempfile.TemporaryDirectory(prefix="duckdb_") as tmp:
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"SET memory_limit='{int(memory_limit_mb)}MB'")
            con.execute(f"SET threads={int(threads)}")
            con.execute(f"SET temp_directory={lit(tmp)}")
            yield con
        finally:
            con.close()


def file_relation(path: Path, all_varchar: bool = True) -> tuple[str, str]:
    """Return (format, DuckDB table-function SQL) for a CSV or Parquet file."""
    suffix = path.suffix.lower()
    posix = lit(path.as_posix())
    if suffix in (".csv", ".tsv", ".txt"):
        flag = "all_varchar=true, " if all_varchar else ""
        return "csv", f"read_csv({posix}, {flag}header=true, null_padding=true)"
    if suffix in (".parquet", ".pq"):
        return "parquet", f"read_parquet({posix})"
    raise ValueError(f"unsupported file type '{path.suffix}' (expected CSV or Parquet)")
