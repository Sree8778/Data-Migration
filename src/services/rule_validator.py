"""AST guardrails for LLM-proposed mapping rules.

Nothing the LLM returns is trusted. A recommendation is checked for:
  1. target exists in the target catalog (`cat_columns`, TARGET systems, given business object);
  2. `transformation_sql` is ONE side-effect-free scalar expression: parsed with sqlglot, no
     semicolons/comments, no DDL/DML/commands/subqueries/parameters, only allow-listed functions
     and only references to known source columns;
  3. confidence >= threshold and no check failed, otherwise the rule is flagged NEEDS_REVIEW.

A rule whose target cannot be resolved, or whose required SQL is missing/unsafe, is not
`persistable`: it cannot be stored in `map_field_rules` without breaking its constraints (or
without storing unsafe text), so the orchestrator reports it instead.
"""

from __future__ import annotations

from typing import Iterable, Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import TargetType
from src.db.models import CatColumn, CatDataSource, CatTable
from src.schemas.mapping_ai import (
    FieldMappingRecommendation,
    ReviewStatus,
    TargetCandidate,
    ValidationResult,
)

REVIEW_CONFIDENCE_THRESHOLD = 0.60
MAX_SQL_LENGTH = 1000
SOURCE_PLACEHOLDER = "source_col"  # the prompt's generic name for the source column


def _classes(*names: str) -> tuple[type, ...]:
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


# Anything that is a statement, a query, or reaches outside the current row.
_FORBIDDEN_NODES = _classes(
    "Create", "Drop", "Alter", "AlterColumn", "AlterRename", "Insert", "Update", "Delete", "Merge",
    "Command", "TruncateTable", "Use", "Set", "SetItem", "Transaction", "Commit", "Rollback",
    "Pragma", "Copy", "Attach", "Detach", "Grant", "Query", "Select", "Subquery", "Union",
    "Intersect", "Except", "With", "Values", "Star", "Placeholder", "Parameter", "SessionParameter",
    "Table", "Lateral", "Window",
)

# Function classes permitted in rules (everything else that is an exp.Func is rejected).
_ALLOWED_FUNCS = _classes(
    "Trim", "Upper", "Lower", "Initcap", "Pad", "Substring", "Concat", "ConcatWs", "Coalesce",
    "Nullif", "Cast", "TryCast", "Case", "If", "Length", "Abs", "Round", "Floor", "Ceil",
    "Replace", "RegexpReplace", "Left", "Right", "Reverse", "Translate", "StrPosition", "Mod",
    "ToChar", "TimeToStr", "StrToDate", "StrToTime", "TsOrDsToDate", "Greatest", "Least",
)
# Anonymous (unrecognised-by-sqlglot) function names that are still harmless.
_ALLOWED_ANONYMOUS = {
    "LPAD", "RPAD", "LTRIM", "RTRIM", "TO_CHAR", "TO_NUMBER", "TO_DATE", "NVL", "NVL2", "INSTR",
    "SUBSTR", "TRANSLATE", "DECODE", "INITCAP",
}


def validate_sql_expression(
    sql: str,
    allowed_columns: Optional[Iterable[str]] = None,
    *,
    allow_columns: bool = True,
    dialect: Optional[str] = None,
) -> tuple[Optional[str], list[str]]:
    """Return (normalised_sql, errors). `normalised_sql` is None when any error is found."""
    errors: list[str] = []
    text = (sql or "").strip()
    if not text:
        return None, ["transformation_sql is empty"]
    if len(text) > MAX_SQL_LENGTH:
        return None, [f"transformation_sql longer than {MAX_SQL_LENGTH} characters"]
    if ";" in text:
        return None, ["semicolons are not allowed in transformation_sql"]
    if "--" in text or "/*" in text or "*/" in text:
        return None, ["SQL comments are not allowed in transformation_sql"]

    try:
        statements = sqlglot.parse(text, read=dialect)
    except SqlglotError as exc:
        return None, [f"SQL does not parse: {str(exc).splitlines()[0][:200]}"]
    if len(statements) != 1 or statements[0] is None:
        return None, ["transformation_sql must be exactly one expression"]
    tree = statements[0]

    allowed = None
    if allowed_columns is not None:
        allowed = {c.lower() for c in allowed_columns} | {SOURCE_PLACEHOLDER}
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            errors.append(f"forbidden SQL construct: {type(node).__name__}")
        elif isinstance(node, exp.Anonymous):
            if str(node.name).upper() not in _ALLOWED_ANONYMOUS:
                errors.append(f"function not allowed: {node.name}")
        elif isinstance(node, exp.Func) and not isinstance(node, _ALLOWED_FUNCS):
            errors.append(f"function not allowed: {type(node).__name__}")
        elif isinstance(node, exp.Column):
            if not allow_columns:
                errors.append(f"column reference '{node.name}' not allowed in a static value")
            elif node.table or node.args.get("db"):
                errors.append(f"qualified column reference not allowed: {node.sql()}")
            elif allowed is not None and node.name.lower() not in allowed:
                errors.append(f"unknown source column referenced: {node.name}")
    if errors:
        return None, sorted(set(errors))
    return tree.sql(dialect=dialect), []


class RuleValidator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_name: str = "BUSINESS_PARTNER",
        review_threshold: float = REVIEW_CONFIDENCE_THRESHOLD,
        dialect: Optional[str] = None,
    ) -> None:
        self._session_factory = session_factory
        self._object_name = object_name
        self._threshold = review_threshold
        self._dialect = dialect

    @property
    def object_name(self) -> str:
        return self._object_name

    def validate(
        self,
        rec: FieldMappingRecommendation,
        *,
        allowed_source_columns: Optional[Iterable[str]] = None,
        candidates: Optional[list[TargetCandidate]] = None,
    ) -> ValidationResult:
        reasons: list[str] = []
        persistable = True

        # Check 1: target exists in the catalog.
        target = self._find_target(rec.target_table, rec.target_column)
        if target is None:
            persistable = False
            reasons.append(
                f"target {rec.target_table}.{rec.target_column} not found in target catalog "
                f"for object '{self._object_name}'"
            )
        elif candidates is not None and not any(c.column_id == target.id for c in candidates):
            reasons.append("target was not among the retrieved candidates")

        # Check 2: SQL guardrails.
        sql_out: Optional[str] = None
        if rec.rule_type in ("SQL_EXPRESSION", "STATIC_VALUE"):
            if rec.transformation_sql is None:
                persistable = False
                reasons.append(f"{rec.rule_type} requires transformation_sql")
            else:
                sql_out, errors = validate_sql_expression(
                    rec.transformation_sql,
                    allowed_source_columns,
                    allow_columns=rec.rule_type != "STATIC_VALUE",
                    dialect=self._dialect,
                )
                if errors:
                    persistable = False
                    reasons += [f"unsafe/invalid SQL: {e}" for e in errors]
        else:
            if rec.transformation_sql is not None:
                _, errors = validate_sql_expression(
                    rec.transformation_sql, allowed_source_columns, dialect=self._dialect
                )
                reasons += [f"unsafe/invalid SQL: {e}" for e in errors]
                reasons.append(f"transformation_sql ignored for {rec.rule_type}")
            if rec.rule_type == "VALUE_MAPPING":
                reasons.append("value lookups are not defined yet")

        # Check 3: confidence.
        if rec.confidence_score < self._threshold:
            reasons.append(f"confidence {rec.confidence_score:.2f} below {self._threshold:.2f}")

        return ValidationResult(
            status=ReviewStatus.NEEDS_REVIEW if reasons else ReviewStatus.OK,
            persistable=persistable,
            target_table_id=target.table_id if target else None,
            target_column_id=target.id if target else None,
            target_is_mandatory=bool(target.is_mandatory) if target else False,
            transformation_sql=sql_out,
            reasons=reasons,
        )

    def _find_target(self, table_name: str, column_name: str) -> Optional[CatColumn]:
        with self._session_factory() as session:
            return session.scalar(
                select(CatColumn)
                .join(CatTable, CatColumn.table_id == CatTable.id)
                .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(
                    CatDataSource.target_type == TargetType.TARGET,
                    CatTable.business_object == self._object_name,
                    func.upper(CatTable.table_name) == table_name.strip().upper(),
                    func.upper(CatColumn.column_name) == column_name.strip().upper(),
                )
            )
