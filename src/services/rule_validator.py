"""AST guardrails for LLM-proposed mapping rules.

Nothing the LLM returns is trusted. A recommendation is checked for:
  1. target exists in the target catalog (`cat_columns`, TARGET systems, given business object);
  2. `transformation_sql` is ONE side-effect-free scalar expression: parsed with sqlglot, no
     semicolons/comments, no DDL/DML/commands/subqueries/parameters, only allow-listed functions
     and only references to known source columns;
  3. confidence >= threshold and no check failed, otherwise the rule is flagged NEEDS_REVIEW.

Beyond the structure, the guardrails look at EVIDENCE when the source column is known: a copied column
whose values are longer than the SAP field, or whose sample values are not keys of the field's SAP check
table (a country written "Germany" going into a field that holds "DE"), is flagged for a human.

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
    SourceColumnContext,
    TargetCandidate,
    ValidationResult,
)
from src.services.domain_provider import DomainProvider, default_domain_provider

REVIEW_CONFIDENCE_THRESHOLD = 0.60
MAX_SQL_LENGTH = 1000
MAX_LENGTH_ARGUMENT = 4000   # no SAP text field is longer; LPAD(x, 2000000000, 'a') would build a 2 GB string for every row
_LENGTH_FUNCS = tuple(getattr(exp, n) for n in ("Pad", "Left", "Right", "Substring") if hasattr(exp, n))
_LENGTH_ANONYMOUS = {"LPAD", "RPAD", "SUBSTR"}
SOURCE_PLACEHOLDER = "source_col"  # the prompt's generic name for the source column

# Functions that change what a value SAYS, not only how it is typed or cut: padding adds characters, replacing and translating rewrite them. A model proposes these with a confident
# reason ("German postal codes have 5 digits, so pad with zeros") that holds for the sample it saw and not for the whole column (4-digit Austrian and Swiss codes become wrong codes).
_PADDING = {"Pad": "adds padding characters", "LPAD": "adds padding characters", "RPAD": "adds padding characters"}
_REWRITING = {"Replace": "replaces characters", "RegexpReplace": "rewrites characters by pattern", "Translate": "swaps characters", "TRANSLATE": "swaps characters"}


def content_changing_functions(sql: str, dialect: Optional[str] = None) -> list[str]:
    """What the expression does to the values themselves, as phrases ("adds padding characters"); empty for a cast, a trim, a case change or a cut."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return []
    found: list[str] = []
    for node in tree.walk():
        name = str(node.name).upper() if isinstance(node, exp.Anonymous) else type(node).__name__
        for table in (_PADDING, _REWRITING):
            if name in table and table[name] not in found:
                found.append(table[name])
    return found


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
    for node in tree.walk():   # a literal length far beyond any SAP field is a resource attack or a mistake, never a mapping
        takes_length = isinstance(node, _LENGTH_FUNCS) or (isinstance(node, exp.Anonymous) and str(node.name).upper() in _LENGTH_ANONYMOUS)
        if not takes_length:
            continue
        args = [v for v in node.args.values() if isinstance(v, exp.Expression)] + [e for v in node.args.values() if isinstance(v, list) for e in v if isinstance(e, exp.Expression)]
        for arg in args:
            number = arg.this if isinstance(arg, exp.Literal) and not arg.is_string else None
            if number is not None and number.lstrip("-").isdigit() and abs(int(number)) > MAX_LENGTH_ARGUMENT:
                errors.append(f"length argument {number} is larger than {MAX_LENGTH_ARGUMENT}")
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
        domain_provider: Optional[DomainProvider] = None,
    ) -> None:
        self._session_factory = session_factory
        self._object_name = object_name
        self._threshold = review_threshold
        self._dialect = dialect
        self._domains = domain_provider or default_domain_provider()

    @property
    def object_name(self) -> str:
        return self._object_name

    def validate(
        self,
        rec: FieldMappingRecommendation,
        *,
        allowed_source_columns: Optional[Iterable[str]] = None,
        candidates: Optional[list[TargetCandidate]] = None,
        source: Optional[SourceColumnContext] = None,
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

        # Check 3: evidence from the source column's profile against the target's length and check table.
        if source is not None and target is not None:
            reasons += self._evidence_reasons(rec, target, source)
        if rec.rule_type == "SQL_EXPRESSION" and sql_out:
            changes = content_changing_functions(sql_out, self._dialect)
            if changes:
                reasons.append(f"the expression changes the values themselves ({' and '.join(changes)}), not only their type: check it against all the data, not just the sample "
                               "(padding a postal code to 5 digits would turn a valid 4-digit code into a wrong one)")

        # Check 4: confidence.
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

    def _evidence_reasons(self, rec: FieldMappingRecommendation, target: CatColumn, source: SourceColumnContext) -> list[str]:
        """Facts about the data that contradict a plain copy. A lookup or an expression is trusted to change the values, so only
        DIRECT_COPY is checked here; the sample dry-run (`MappingPreflightService`) tests every rule type on real rows."""
        if rec.rule_type != "DIRECT_COPY":
            return []
        out: list[str] = []
        if target.char_max_length and source.char_max_length and source.char_max_length > target.char_max_length:
            out.append(f"source values reach {source.char_max_length} characters but {target.column_name} holds {target.char_max_length}")
        if target.check_table and source.evidence_values:
            domain = self._domains.get_domain(target.check_table)
            if domain is not None:
                values = [v.strip() for v in source.evidence_values if v and v.strip()]
                valid = [v for v in values if v in domain or v.upper() in domain]
                if values and len(valid) < len(values):
                    out.append(f"only {len(valid)} of {len(values)} sample values are valid {target.check_table} keys, so a lookup may be needed")
        return out

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
