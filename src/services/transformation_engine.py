"""Deterministic transformation engine: compiles `map_field_rules` into one DuckDB query.

All row-level work happens inside DuckDB (vectorised, memory-capped at 1.5 GB, spills to disk).
Python only compiles SQL, loads the (small) lookup tables, and streams lineage to PostgreSQL via
COPY - there is no per-row Python loop anywhere.

Semantics
* Source values are read as text. Blank / whitespace-only values are NULL.
* DIRECT_COPY trims and conforms to the target type; STATIC_VALUE / SQL_EXPRESSION evaluate the
  rule's expression (re-validated with the Module 3 AST guardrails at compile time, `CAST` is
  executed as `TRY_CAST` so one dirty value cannot abort the batch); VALUE_MAPPING translates
  through `map_value_lookups` (trimmed, case-insensitive match).
* Every result is conformed to the target column: type conversion, max length (never silently
  truncated) and mandatory-ness. A violated cell becomes NULL and carries an error message; its
  row is marked `_TRANSFORM_STATUS = 'FAILED'` but is kept in staging for triage.
* Staging columns are named `<TARGET_TABLE>_<TARGET_COLUMN>`; extra source columns requested via
  `passthrough_columns` are copied raw as `SRC_<name>` (used by the deduplication engine).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlglot import exp

from src.db.enums import MappingStatus, RuleType
from src.db.models import CatColumn, CatTable, MapFieldRule, MapTableRule
from src.schemas.execution import (
    LineageMode,
    LookupFallback,
    RuleSummary,
    TransformationResult,
)
from src.services._duckdb_util import (
    DEFAULT_MEMORY_LIMIT_MB,
    bounded_connection,
    file_relation,
    lit,
    q,
)
from src.services.lookup_service import LookupService
from src.services.metadata_service import MappingValidationError, NotFoundError
from src.services.rule_validator import SOURCE_PLACEHOLDER, validate_sql_expression

PK_COLUMN = "_SRC_PK"
STATUS_COLUMN = "_TRANSFORM_STATUS"
ERRORS_COLUMN = "_TRANSFORM_ERRORS"
_ERR_VALUE_CHARS = 60

_STRING_TYPES = {"CHAR", "VARCHAR", "VARCHAR2", "NVARCHAR", "STRING", "SSTRING", "LANG", "CUKY", "CLNT",
                 "UNIT", "LCHR", "TEXT", "DATS", "TIMS"}
_INT_TYPES = {"INT", "INT1", "INT2", "INT4", "INT8", "INTEGER", "SMALLINT", "BIGINT"}
_DECIMAL_TYPES = {"DEC", "DECIMAL", "CURR", "QUAN", "NUMBER", "NUMERIC", "FLTP", "FLOAT", "DOUBLE"}


class TransformationCompileError(Exception):
    """The mapping cannot be compiled safely (bad SQL, missing source column, ambiguous lookup...)."""


@dataclass(frozen=True)
class _TypeSpec:
    kind: str  # string | numc | int | decimal | date | timestamp
    duck: str
    max_len: Optional[int]
    label: str


def _type_spec(col: CatColumn) -> _TypeSpec:
    dtype = (col.data_type or "").upper().split("(")[0].strip()
    length = col.char_max_length
    if dtype == "NUMC":
        return _TypeSpec("numc", "VARCHAR", length, f"NUMC({length})" if length else "NUMC")
    if dtype in _INT_TYPES:
        return _TypeSpec("int", "BIGINT", None, dtype)
    if dtype in _DECIMAL_TYPES:
        if col.numeric_precision:
            p, s = col.numeric_precision, col.numeric_scale or 0
            return _TypeSpec("decimal", f"DECIMAL({p},{s})", None, f"{dtype}({p},{s})")
        return _TypeSpec("decimal", "DOUBLE", None, dtype)
    if dtype == "DATE":
        return _TypeSpec("date", "DATE", None, "DATE")
    if dtype in ("TIMESTAMP", "DATETIME"):
        return _TypeSpec("timestamp", "TIMESTAMP", None, "TIMESTAMP")
    # CHAR-like and anything unrecognised: text with the catalogued maximum length
    return _TypeSpec("string", "VARCHAR", length, f"{dtype or 'CHAR'}({length})" if length else dtype or "CHAR")


@dataclass
class _Rule:
    idx: int
    rule_id: int
    rule_type: RuleType
    source_col: Optional[str]
    target_table: str
    target_col: str
    logic: Optional[str]
    mandatory: bool
    spec: _TypeSpec
    lookup: Optional[dict[str, str]] = None
    expr_sql: Optional[str] = None

    @property
    def target(self) -> str:
        return f"{self.target_table}.{self.target_col}"

    @property
    def staging(self) -> str:
        return f"{self.target_table}_{self.target_col}"


class TransformationEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        lookup_service: Optional[LookupService] = None,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        threads: int = 2,
    ) -> None:
        self._session_factory = session_factory
        self._lookups = lookup_service or LookupService(session_factory)
        self._memory_limit_mb = memory_limit_mb
        self._threads = threads

    # ------------------------------------------------------------------ public API
    def transform(
        self,
        mapping_set_id: int,
        source_path: str | Path,
        output_path: str | Path,
        *,
        run_id: Optional[int] = None,
        source_pk_columns: Optional[list[str]] = None,
        passthrough_columns: Optional[list[str]] = None,
        lookup_fallback: LookupFallback = LookupFallback.FLAG_FAILED,
        lookup_default: Optional[str] = None,
        lineage_mode: LineageMode = LineageMode.ALL,
        audited_targets: Optional[Iterable[str]] = None,
    ) -> TransformationResult:
        """Run a mapping set over a CSV/Parquet source and write conformed staging Parquet.

        `run_id`: when given (and lineage_mode != NONE) cell lineage is written to `mig_cell_lineage`.
        `audited_targets`: optional 'TABLE.COLUMN' names restricting which rules get lineage.
        """
        source, output = Path(source_path), Path(output_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        if output.suffix.lower() not in (".parquet", ".pq"):
            raise ValueError("output_path must be a .parquet file")
        output.parent.mkdir(parents=True, exist_ok=True)
        if lookup_fallback == LookupFallback.USE_DEFAULT and not lookup_default:
            raise ValueError("USE_DEFAULT fallback needs lookup_default")

        rules, catalog_pk = self._load_rules(mapping_set_id)
        audited = {a.upper() for a in audited_targets} if audited_targets is not None else None

        with bounded_connection(self._memory_limit_mb, self._threads) as con:
            _fmt, relation = file_relation(source)
            file_cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]
            lower_cols = {c.lower(): c for c in file_cols}
            self._compile(rules, lower_cols, file_cols, lookup_fallback, lookup_default)

            pk_cols, strategy = self._resolve_pk(source_pk_columns, catalog_pk, lower_cols)
            passthrough = self._resolve_passthrough(passthrough_columns, lower_cols)

            self._create_source_view(con, relation, file_cols)
            self._load_lookup_tables(con, rules)
            self._materialise(con, rules, pk_cols, passthrough, lookup_fallback, lookup_default)

            failed_cells = self._count_failed_cells(con, rules)
            total, failed_rows = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE _ec > 0) FROM x2"
            ).fetchone()
            self._write_staging(con, rules, passthrough, output)

            lineage_rows = 0
            if run_id is not None and lineage_mode != LineageMode.NONE:
                audited_rules = [r for r in rules if audited is None or r.target.upper() in audited]
                if audited_rules:
                    lineage_rows = self._write_lineage(con, run_id, audited_rules, lineage_mode)

        return TransformationResult(
            mapping_set_id=mapping_set_id,
            source_path=str(source),
            output_path=str(output),
            pk_strategy=strategy,
            source_records=int(total),
            ok_records=int(total - failed_rows),
            failed_records=int(failed_rows),
            rules=[
                RuleSummary(
                    rule_id=r.rule_id, rule_type=r.rule_type.value, source_column=r.source_col,
                    target=r.target, staging_column=r.staging, failed_cells=failed_cells[r.idx],
                )
                for r in rules
            ],
            lineage_rows=lineage_rows,
            passthrough_columns=[f"SRC_{c}" for c in passthrough],
        )

    # ------------------------------------------------------------------ rule loading / compiling
    def _load_rules(self, mapping_set_id: int) -> tuple[list[_Rule], list[str]]:
        with self._session_factory() as session:
            mapping = session.scalar(
                select(MapTableRule).where(MapTableRule.id == mapping_set_id)
                .options(selectinload(MapTableRule.source_table).selectinload(CatTable.columns))
            )
            if mapping is None:
                raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
            if mapping.status == MappingStatus.REJECTED:
                raise MappingValidationError(f"mapping set {mapping_set_id} is REJECTED and cannot be executed")
            catalog_pk = [c.column_name for c in mapping.source_table.columns if c.is_primary_key]

            rows = session.execute(
                select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id).order_by(MapFieldRule.id)
            ).scalars().all()
            if not rows:
                raise MappingValidationError(f"mapping set {mapping_set_id} has no field rules")
            rules: list[_Rule] = []
            for i, fr in enumerate(rows):
                target = session.get(CatColumn, fr.target_column_id)
                table = session.get(CatTable, target.table_id)
                source = session.get(CatColumn, fr.source_column_id) if fr.source_column_id else None
                rules.append(_Rule(
                    idx=i, rule_id=fr.id, rule_type=fr.rule_type,
                    source_col=source.column_name if source else None,
                    target_table=table.table_name, target_col=target.column_name,
                    logic=fr.transformation_logic,
                    mandatory=bool(fr.is_mandatory_target or target.is_mandatory),
                    spec=_type_spec(target),
                ))
            return rules, catalog_pk

    def _compile(self, rules, lower_cols, file_cols, fallback, default) -> None:
        missing: list[str] = []
        for r in rules:
            if r.source_col is not None:
                actual = lower_cols.get(r.source_col.lower())
                if actual is None:
                    missing.append(f"{r.source_col} (rule {r.rule_id})")
                else:
                    r.source_col = actual
            elif r.rule_type in (RuleType.DIRECT_COPY, RuleType.VALUE_MAPPING):
                raise TransformationCompileError(f"rule {r.rule_id} ({r.rule_type.value}) has no source column")
        if missing:
            raise TransformationCompileError(f"source file is missing columns: {', '.join(missing)}")

        for r in rules:
            if r.rule_type in (RuleType.SQL_EXPRESSION, RuleType.STATIC_VALUE):
                r.expr_sql = self._render_expression(r, file_cols)
            elif r.rule_type == RuleType.VALUE_MAPPING:
                r.lookup = self._lookups.get_normalized_lookup(r.rule_id)  # raises on ambiguity
            elif r.rule_type == RuleType.PYTHON_SNIPPET:
                raise TransformationCompileError(
                    f"rule {r.rule_id}: PYTHON_SNIPPET rules are not executable by the deterministic engine")

    @staticmethod
    def _render_expression(rule: _Rule, file_cols: list[str]) -> str:
        """Re-validate (defence in depth: the DB row may have been edited) and render for DuckDB."""
        if not rule.logic:
            raise TransformationCompileError(f"rule {rule.rule_id} ({rule.rule_type.value}) has no transformation_logic")
        static = rule.rule_type == RuleType.STATIC_VALUE
        normalized, errors = validate_sql_expression(rule.logic, file_cols, allow_columns=not static)
        if errors:
            raise TransformationCompileError(f"rule {rule.rule_id} rejected by SQL guardrails: {'; '.join(errors)}")
        has_real_placeholder = any(c.lower() == SOURCE_PLACEHOLDER for c in file_cols)

        def rewrite(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not has_real_placeholder and node.name.lower() == SOURCE_PLACEHOLDER:
                if rule.source_col is None:
                    raise TransformationCompileError(f"rule {rule.rule_id} uses source_col but has no source column")
                return exp.column(rule.source_col, quoted=True)
            if isinstance(node, exp.Cast):  # dirty values must yield NULL, not abort the batch
                return exp.TryCast(this=node.this, to=node.args["to"])
            return node

        tree = sqlglot.parse_one(normalized).transform(rewrite)
        return tree.sql(dialect="duckdb", identify=True)

    @staticmethod
    def _resolve_pk(param, catalog_pk, lower_cols) -> tuple[list[str], str]:
        if param:
            missing = [c for c in param if c.lower() not in lower_cols]
            if missing:
                raise TransformationCompileError(f"source_pk_columns not in source file: {missing}")
            return [lower_cols[c.lower()] for c in param], "PARAMETER"
        present = [lower_cols[c.lower()] for c in catalog_pk if c.lower() in lower_cols]
        if catalog_pk and len(present) == len(catalog_pk):
            return present, "CATALOG_PK"
        return [], "ROW_NUMBER"

    @staticmethod
    def _resolve_passthrough(columns, lower_cols) -> list[str]:
        out = []
        for c in columns or []:
            if c.lower() not in lower_cols:
                raise TransformationCompileError(f"passthrough column not in source file: {c}")
            out.append(lower_cols[c.lower()])
        return out

    # ------------------------------------------------------------------ execution
    @staticmethod
    def _create_source_view(con, relation: str, file_cols: list[str]) -> None:
        cols = ", ".join(
            f"CASE WHEN trim(CAST({q(c)} AS VARCHAR)) = '' THEN NULL ELSE CAST({q(c)} AS VARCHAR) END AS {q(c)}"
            for c in file_cols
        )
        con.execute(f'CREATE VIEW src AS SELECT row_number() OVER () AS "__rn", {cols} FROM {relation}')

    @staticmethod
    def _load_lookup_tables(con, rules: list[_Rule]) -> None:
        for r in rules:
            if r.rule_type != RuleType.VALUE_MAPPING:
                continue
            con.execute(f'CREATE TEMP TABLE "__lk{r.idx}" ("__k" VARCHAR, "__v" VARCHAR)')
            if r.lookup:
                con.executemany(f'INSERT INTO "__lk{r.idx}" VALUES (?, ?)', list(r.lookup.items()))

    def _materialise(self, con, rules, pk_cols, passthrough, fallback, default) -> None:
        """x1: raw + value + lookup-error per rule.  x2: conformed value + error per rule."""
        if pk_cols:
            nulls = " OR ".join(f"{q(c)} IS NULL" for c in pk_cols)
            joined = ", ".join(f"trim({q(c)})" for c in pk_cols)
            pk_expr = f"CASE WHEN {nulls} THEN NULL ELSE concat_ws('|', {joined}) END"
        else:
            pk_expr = "NULL"
        select = [f"COALESCE({pk_expr}, 'ROW:' || CAST(\"__rn\" AS VARCHAR)) AS {q(PK_COLUMN)}", '"__rn"']
        joins = []
        for r in rules:
            i, col = r.idx, q(r.source_col) if r.source_col else None
            select.append(f"CAST({col} AS VARCHAR) AS r{i}" if col else f"CAST(NULL AS VARCHAR) AS r{i}")
            lerr = "CAST(NULL AS VARCHAR)"
            if r.rule_type == RuleType.DIRECT_COPY:
                value = f"nullif(trim({col}), '')"
            elif r.rule_type == RuleType.VALUE_MAPPING:
                joins.append(f'LEFT JOIN "__lk{i}" ON upper(trim({col})) = "__lk{i}"."__k"')
                hit = f'"__lk{i}"."__v"'
                if fallback == LookupFallback.PASS_THROUGH:
                    miss = f"trim({col})"
                elif fallback == LookupFallback.USE_DEFAULT:
                    miss = lit(default)
                else:
                    miss = "CAST(NULL AS VARCHAR)"
                    lerr = (f"CASE WHEN {col} IS NOT NULL AND {hit} IS NULL "
                            f"THEN 'LOOKUP_FAILED: no mapping for ''' || left(trim({col}), {_ERR_VALUE_CHARS}) || '''' END")
                value = f"CASE WHEN {col} IS NULL THEN NULL WHEN {hit} IS NOT NULL THEN {hit} ELSE {miss} END"
            else:  # SQL_EXPRESSION / STATIC_VALUE
                value = f"CAST(({r.expr_sql}) AS VARCHAR)"
            select.append(f"{value} AS v{i}")
            select.append(f"{lerr} AS l{i}")
        select += [f"CAST({q(c)} AS VARCHAR) AS {q('SRC_' + c)}" for c in passthrough]
        con.execute(f"CREATE TEMP TABLE x1 AS SELECT {', '.join(select)} FROM src {' '.join(joins)}")

        carry = [q(PK_COLUMN), '"__rn"'] + [q("SRC_" + c) for c in passthrough]
        stage2 = list(carry)
        for r in rules:
            i, s = r.idx, r.spec
            v = f"v{i}"
            stage2.append(f"r{i}")
            if s.kind == "string":
                typed = f"CASE WHEN length({v}) > {s.max_len} THEN NULL ELSE {v} END" if s.max_len else v
                conv_err = f"'LENGTH_EXCEEDED: value has ' || length({v}) || ' chars, {s.label} allows {s.max_len}'"
            elif s.kind == "numc":
                cond = f"NOT regexp_full_match({v}, '[0-9]+')"
                if s.max_len:
                    cond += f" OR length({v}) > {s.max_len}"
                typed = f"CASE WHEN {cond} THEN NULL ELSE {v} END"
                conv_err = f"'CONVERSION_FAILED: ''' || left({v}, {_ERR_VALUE_CHARS}) || ''' is not valid {s.label}'"
            else:
                typed = f"TRY_CAST({v} AS {s.duck})"
                conv_err = f"'CONVERSION_FAILED: ''' || left({v}, {_ERR_VALUE_CHARS}) || ''' is not valid {s.label}'"
            stage2.append(f"{typed} AS t{i}")
            mandatory = f"WHEN t{i} IS NULL THEN 'MANDATORY_EMPTY: {r.target} is mandatory'" if r.mandatory else ""
            stage2.append(
                f"COALESCE(l{i}, CASE WHEN {v} IS NOT NULL AND t{i} IS NULL THEN {conv_err} {mandatory} END) AS e{i}"
            )
        err_count = " + ".join(f"CAST(e{r.idx} IS NOT NULL AS INTEGER)" for r in rules)
        stage2.append(f"({err_count}) AS _ec")
        stage2.append(
            "concat_ws('; ', " + ", ".join(
                f"CASE WHEN e{r.idx} IS NOT NULL THEN {lit(r.target + ': ')} || e{r.idx} END" for r in rules
            ) + ") AS _errs"
        )
        con.execute(f"CREATE TEMP TABLE x2 AS SELECT {', '.join(stage2)} FROM x1")

    @staticmethod
    def _count_failed_cells(con, rules: list[_Rule]) -> dict[int, int]:
        row = con.execute(
            "SELECT " + ", ".join(f"count(e{r.idx})" for r in rules) + " FROM x2"
        ).fetchone()
        return {r.idx: int(n) for r, n in zip(rules, row)}

    @staticmethod
    def _write_staging(con, rules, passthrough, output: Path) -> None:
        cols = [q(PK_COLUMN)]
        cols += [f"t{r.idx} AS {q(r.staging)}" for r in rules]
        cols += [q("SRC_" + c) for c in passthrough]
        cols.append(f"CASE WHEN _ec > 0 THEN 'FAILED' ELSE 'OK' END AS {q(STATUS_COLUMN)}")
        cols.append(f"nullif(_errs, '') AS {q(ERRORS_COLUMN)}")
        con.execute(
            f"COPY (SELECT {', '.join(cols)} FROM x2 ORDER BY \"__rn\") TO {lit(output.as_posix())} (FORMAT parquet)"
        )

    def _write_lineage(self, con, run_id: int, rules: list[_Rule], mode: LineageMode) -> int:
        parts = []
        for r in rules:
            i = r.idx
            where = ""
            if mode == LineageMode.EXCEPTIONS:
                where = f" WHERE r{i} IS DISTINCT FROM CAST(t{i} AS VARCHAR) OR e{i} IS NOT NULL"
            parts.append(
                f"SELECT {int(run_id)} AS run_id, {q(PK_COLUMN)} AS source_pk_value, "
                f"CAST(NULL AS VARCHAR) AS target_pk_value, {lit(r.target)} AS target_column_name, "
                f"r{i} AS source_raw_value, CAST(t{i} AS VARCHAR) AS transformed_value, "
                f"{int(r.rule_id)} AS rule_applied_id, false AS loaded_successfully, e{i} AS error_message "
                f"FROM x2{where}"
            )
        fd, tmp_name = tempfile.mkstemp(prefix="lineage_", suffix=".csv")
        os.close(fd)
        try:
            con.execute(
                f"COPY ({' UNION ALL '.join(parts)}) TO {lit(Path(tmp_name).as_posix())} (FORMAT csv, HEADER false)"
            )
            with self._session_factory.begin() as session, open(tmp_name, "r", encoding="utf-8", newline="") as fh:
                cursor = session.connection().connection.cursor()
                try:
                    cursor.copy_expert(
                        "COPY mig_cell_lineage (run_id, source_pk_value, target_pk_value, target_column_name, "
                        "source_raw_value, transformed_value, rule_applied_id, loaded_successfully, error_message) "
                        "FROM STDIN WITH (FORMAT csv)",
                        fh,
                    )
                    return int(cursor.rowcount)
                finally:
                    cursor.close()
        finally:
            os.remove(tmp_name)
