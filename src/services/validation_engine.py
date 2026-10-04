"""Pre-load validation of Module 4 staging output against the SAP target specification.

Everything row-level runs as vectorised DuckDB SQL (1.5 GB cap, spill to disk). For each mapped
target column one "failure kind" is derived per row, in priority order:

    LOOKUP_FAILED  value is the LOOKUP_FAILED sentinel, or Module 4 recorded a failed lookup
    LENGTH         string longer than char_max_length (or Module 4 recorded LENGTH_EXCEEDED)
    CONVERSION     Module 4 recorded CONVERSION_FAILED
    MANDATORY      mandatory target is NULL / empty / whitespace
    CHECKTABLE     value is not a key of the target's check table (domain provider)

Module 4 turns a failing cell into NULL and writes the cause to `_TRANSFORM_ERRORS`; reading both
means the true cause is reported (a too-long name is LENGTH, not a confusing MANDATORY).

Records flagged `is_duplicate_child` are segregated into `duplicates.parquet`: they are neither
evaluated nor counted as defects and never reach the valid payload. They appear in the report
summary only. Outputs: valid.parquet (loadable), rejected.parquet (+ `_VALIDATION_ERRORS`),
duplicates.parquet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import CatColumn, CatTable, MapFieldRule, MapTableRule
from src.schemas.validation import (
    FailureBreakdown,
    FailureKind,
    ValidationReport,
    severity_for_signature,
    signature_for,
)
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, lit, q
from src.services.domain_provider import DomainProvider, default_domain_provider
from src.services.metadata_service import NotFoundError
from src.services.transformation_engine import ERRORS_COLUMN, PK_COLUMN, _type_spec  # Module 4 conventions

DUPLICATE_COLUMN = "is_duplicate_child"
VALIDATION_ERRORS_COLUMN = "_VALIDATION_ERRORS"
_SAMPLE_SIZE = 5


@dataclass
class _Spec:
    idx: int
    rule_id: int
    table: str
    column: str
    staging: str
    mandatory: bool
    kind_of_type: str
    max_len: Optional[int]
    check_table: Optional[str]
    domain: Optional[frozenset[str]] = None
    name_part: str = ""

    @property
    def target(self) -> str:
        return f"{self.table}.{self.column}"


class ValidationEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        domain_provider: Optional[DomainProvider] = None,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        threads: int = 2,
    ) -> None:
        self._session_factory = session_factory
        self._domains = domain_provider or default_domain_provider()
        self._memory_limit_mb = memory_limit_mb
        self._threads = threads

    # ------------------------------------------------------------------ public API
    def validate(
        self,
        mapping_set_id: int,
        staging_path: str | Path,
        output_dir: Optional[str | Path] = None,
        run_id: Optional[int] = None,
    ) -> ValidationReport:
        """Validate a staging Parquet (Module 4 output, before or after deduplication)."""
        source = Path(staging_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        specs = self._load_specs(mapping_set_id)
        out_dir = Path(output_dir) if output_dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        with bounded_connection(self._memory_limit_mb, self._threads) as con:
            columns = [r[0] for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet({lit(source.as_posix())})").fetchall()]
            self._check_columns(specs, columns)
            con.execute(
                f'CREATE TEMP TABLE stg AS SELECT CAST(row_number() OVER () AS BIGINT) AS "__id", * '
                f"FROM read_parquet({lit(source.as_posix())})"
            )
            checked, unchecked = self._load_domains(con, specs)
            self._derive_failures(con, specs, columns)

            total, dups, evaluated, defective = (int(x) for x in con.execute(
                'SELECT count(*), count(*) FILTER (WHERE "__dup"), count(*) FILTER (WHERE NOT "__dup"), '
                'count(*) FILTER (WHERE NOT "__dup" AND "__fail") FROM chk'
            ).fetchone())
            failures = self._breakdown(con, specs)
            paths = self._write_outputs(con, columns, out_dir) if out_dir else {}

        valid = evaluated - defective
        return ValidationReport(
            run_id=run_id,
            mapping_set_id=mapping_set_id,
            staging_path=str(source),
            input_records=total,
            duplicate_children_excluded=dups,
            evaluated_records=evaluated,
            valid_records=valid,
            defective_records=defective,
            quality_score=round(100.0 * valid / evaluated, 2) if evaluated else 0.0,
            failures=failures,
            checked_domains=checked,
            unchecked_check_tables=unchecked,
            valid_path=paths.get("valid"),
            rejected_path=paths.get("rejected"),
            duplicates_path=paths.get("duplicates"),
            validated_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------ specification
    def _load_specs(self, mapping_set_id: int) -> list[_Spec]:
        with self._session_factory() as session:
            if session.get(MapTableRule, mapping_set_id) is None:
                raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
            rules = session.scalars(
                select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id).order_by(MapFieldRule.id)
            ).all()
            if not rules:
                raise NotFoundError(f"mapping set {mapping_set_id} has no field rules")
            specs: list[_Spec] = []
            for i, rule in enumerate(rules):
                col = session.get(CatColumn, rule.target_column_id)
                table = session.get(CatTable, col.table_id)
                ts = _type_spec(col)
                specs.append(_Spec(
                    idx=i, rule_id=rule.id, table=table.table_name, column=col.column_name,
                    staging=f"{table.table_name}_{col.column_name}",
                    mandatory=bool(col.is_mandatory or rule.is_mandatory_target),
                    kind_of_type=ts.kind,
                    max_len=ts.max_len if ts.kind in ("string", "numc") else None,
                    check_table=col.check_table,
                ))
        names = [s.column for s in specs]
        for s in specs:  # table-qualify only when the bare column name is ambiguous
            s.name_part = f"{s.table}_{s.column}" if names.count(s.column) > 1 else s.column
        return specs

    @staticmethod
    def _check_columns(specs: list[_Spec], columns: list[str]) -> None:
        missing = [s.staging for s in specs if s.staging not in columns]
        if PK_COLUMN not in columns:
            missing.append(PK_COLUMN)
        if missing:
            raise ValueError(f"staging dataset is missing columns: {missing}")

    def _load_domains(self, con, specs: list[_Spec]) -> tuple[list[str], list[str]]:
        checked: set[str] = set()
        unchecked: set[str] = set()
        for s in specs:
            if not s.check_table:
                continue
            domain = self._domains.get_domain(s.check_table)
            if domain is None:
                unchecked.add(s.check_table)
                continue
            s.domain = domain
            checked.add(s.check_table)
            con.execute(f'CREATE TEMP TABLE "dom_{s.idx}" AS SELECT unnest(?) AS v', [sorted(domain)])
        return sorted(checked), sorted(unchecked)

    # ------------------------------------------------------------------ SQL
    @staticmethod
    def _kind_sql(s: _Spec, has_errors: bool) -> str:
        c = q(s.staging)
        v = f"CAST({c} AS VARCHAR)"
        err = f"coalesce({q(ERRORS_COLUMN)}, '')" if has_errors else "''"
        tag = f"{s.target}: "

        def seen(code: str) -> str:
            return f"contains({err}, {lit(tag + code)})"

        whens = [f"WHEN {v} = 'LOOKUP_FAILED' OR {seen('LOOKUP_FAILED')} THEN '{FailureKind.LOOKUP.value}'"]
        length = f"{seen('LENGTH_EXCEEDED')}"
        if s.max_len:
            length += f" OR length({v}) > {s.max_len}"
        whens.append(f"WHEN {length} THEN '{FailureKind.LENGTH.value}'")
        whens.append(f"WHEN {seen('CONVERSION_FAILED')} THEN '{FailureKind.CONVERSION.value}'")
        if s.mandatory:
            whens.append(f"WHEN {c} IS NULL OR trim({v}) = '' THEN '{FailureKind.MANDATORY.value}'")
        if s.domain is not None:
            whens.append(
                f"WHEN {c} IS NOT NULL AND trim({v}) <> '' AND {v} NOT IN (SELECT v FROM \"dom_{s.idx}\") "
                f"THEN '{FailureKind.CHECKTABLE.value}'"
            )
        return "CASE " + " ".join(whens) + " END"

    def _derive_failures(self, con, specs: list[_Spec], columns: list[str]) -> None:
        has_errors = ERRORS_COLUMN in columns
        dup = f"coalesce({q(DUPLICATE_COLUMN)}, false)" if DUPLICATE_COLUMN in columns else "false"
        kinds = ", ".join(f'{self._kind_sql(s, has_errors)} AS "__k{s.idx}"' for s in specs)
        con.execute(f'CREATE TEMP TABLE chk0 AS SELECT *, {dup} AS "__dup", {kinds} FROM stg')
        any_fail = " OR ".join(f'"__k{s.idx}" IS NOT NULL' for s in specs)
        sigs = ", ".join(
            f"CASE WHEN \"__k{s.idx}\" IS NOT NULL THEN {lit('ERR_')} || \"__k{s.idx}\" || {lit('_' + s.name_part.upper())} END"
            for s in specs
        )
        con.execute(f'CREATE TEMP TABLE chk AS SELECT *, ({any_fail}) AS "__fail", concat_ws(\'; \', {sigs}) AS "__sigs" FROM chk0')

    def _breakdown(self, con, specs: list[_Spec]) -> list[FailureBreakdown]:
        pk = q(PK_COLUMN)
        parts = [
            f'SELECT {s.idx} AS idx, "__k{s.idx}" AS kind, count(*) AS n, '
            f'list(CAST({pk} AS VARCHAR) ORDER BY "__id")[1:{_SAMPLE_SIZE}] AS sample '
            f'FROM chk WHERE NOT "__dup" AND "__k{s.idx}" IS NOT NULL GROUP BY "__k{s.idx}"'
            for s in specs
        ]
        by_idx = {s.idx: s for s in specs}
        out: list[FailureBreakdown] = []
        for idx, kind, n, sample in con.execute(" UNION ALL ".join(parts)).fetchall():
            s = by_idx[idx]
            fk = FailureKind(kind)
            signature = signature_for(fk, s.name_part)
            out.append(FailureBreakdown(
                signature=signature, kind=fk, severity=severity_for_signature(signature), target=s.target,
                field_rule_id=s.rule_id, violation_count=int(n), sample_failing_pks=[str(x) for x in sample],
            ))
        out.sort(key=lambda f: (-f.violation_count, f.signature))
        return out

    @staticmethod
    def _write_outputs(con, columns: list[str], out_dir: Path) -> dict[str, str]:
        cols = ", ".join(q(c) for c in columns)
        paths = {name: out_dir / f"{name}.parquet" for name in ("valid", "rejected", "duplicates")}
        queries = {
            "valid": f'SELECT {cols} FROM chk WHERE NOT "__dup" AND NOT "__fail" ORDER BY "__id"',
            "rejected": f'SELECT {cols}, nullif("__sigs", \'\') AS {q(VALIDATION_ERRORS_COLUMN)} FROM chk '
                        f'WHERE NOT "__dup" AND "__fail" ORDER BY "__id"',
            "duplicates": f'SELECT {cols} FROM chk WHERE "__dup" ORDER BY "__id"',
        }
        for name, sql in queries.items():
            con.execute(f"COPY ({sql}) TO {lit(paths[name].as_posix())} (FORMAT parquet)")
        return {name: str(p) for name, p in paths.items()}
