"""Row-exclusion rules: leave deleted, test and dummy records behind, with a reason, and account for every one of them.

A legacy extract always contains rows nobody wants in the new system: customers flagged for deletion, "TEST CUSTOMER 12", "DO NOT USE". Without
a way to say so they pass through as perfectly valid records. This module lets a steward state, per mapping set, which rows to leave out:

  * rules are stored in the workspace (one JSON file per mapping set; the database schema is untouched) and, like every other rule, can only be
    changed while the mapping set is a DRAFT: once it is submitted and approved the set of excluded rows is part of what was approved
  * a rule is a column, an operator (equals / in / is_blank / starts_with / contains / regex), the value(s), and a mandatory reason
  * at run time the source is split BEFORE transformation into an included file and an excluded file; the excluded rows are kept (with the
    rule and reason that removed each) so the decision is auditable, and the reconciliation certificate shows them as their own line, so
    "extracted = excluded + transformed + ..." still adds up with zero variance
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.schemas.exclusion import ExclusionResult, ExclusionRule, ExclusionRuleCount, ExclusionRuleInput, ExclusionSuggestion
from src.services._duckdb_util import bounded_connection, file_relation, lit, q

EXCLUDED_BY = "_EXCLUDED_BY"
EXCLUDED_REASON = "_EXCLUDED_REASON"
APPLIED_FILE = "exclusions_applied.json"
_FLAG_COLUMN = re.compile(r"(^|_)(LOEVM|LOEKZ|DEL(ETE|ETED)?(_?(FLAG|IND|MARK))?|INACTIVE|ARCHIVED?|OBSOLETE|CANCEL(L?ED)?)(_|$)", re.IGNORECASE)
_NAME_COLUMN = re.compile(r"(^|_)(NAME\d?|CUST_?NAME|DESCR(IPTION)?|SORTL|MATCHCODE|TITLE)(_|$)", re.IGNORECASE)
_FLAG_VALUES = {"X", "1", "Y", "YES", "TRUE", "J", "D", "T"}
_TEST_PATTERN = r"(^|[^a-z])(test|dummy|do not use|delete me)"
MAX_EXAMPLES = 3


class ExclusionError(ValueError):
    """A rule that cannot be applied (unknown column, unusable pattern, or it would remove every row)."""


# ------------------------------------------------------------------ SQL
def _text(column: str) -> str:
    return f"trim(CAST({q(column)} AS VARCHAR))"


def predicate(rule: ExclusionRule | ExclusionRuleInput, column: str) -> str:
    """SQL that is true for a row the rule removes. `column` is the file's real spelling of the column; values are bound as literals."""
    if rule.operator == "is_blank":
        return f"({q(column)} IS NULL OR {_text(column)} = '')"
    fold = rule.ignore_case and rule.operator != "regex"
    value = f"lower({_text(column)})" if fold else _text(column)
    vals = [v.strip().lower() if fold else v.strip() for v in rule.values]
    if rule.operator == "equals":
        return f"{value} = {lit(vals[0])}"
    if rule.operator == "in":
        return f"{value} IN ({', '.join(lit(v) for v in vals)})"
    if rule.operator == "starts_with":
        return f"starts_with({value}, {lit(vals[0])})"
    if rule.operator == "contains":
        return f"contains({value}, {lit(vals[0])})"
    return f"regexp_matches({_text(column)}, {lit(rule.values[0])}{', ' + lit('i') if rule.ignore_case else ''})"  # regex: RE2, linear time


def _file_columns(con, relation: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]


def _resolve(columns: list[str], name: str) -> str:
    lowered = {c.lower(): c for c in columns}
    if name.strip().lower() not in lowered:
        raise ExclusionError(f"column '{name}' is not in the landed file (columns: {', '.join(columns[:12])}{'...' if len(columns) > 12 else ''})")
    return lowered[name.strip().lower()]


def validate_rule(source: Path, rule: ExclusionRuleInput) -> str:
    """Check the column exists and a regex compiles; returns the file's spelling of the column."""
    with bounded_connection(256) as con:
        _, relation = file_relation(source)
        column = _resolve(_file_columns(con, relation), rule.column)
        if rule.operator == "regex":
            try:
                con.execute(f"SELECT regexp_matches('', {lit(rule.values[0])})")
            except Exception as exc:  # noqa: BLE001 - DuckDB reports the RE2 error
                raise ExclusionError(f"the pattern is not a valid regular expression: {str(exc).splitlines()[0][:160]}") from exc
        return column


# ------------------------------------------------------------------ applying
def apply_exclusions(
    source: Path, rules: list[ExclusionRule], included_path: Optional[Path] = None, excluded_path: Optional[Path] = None, memory_mb: int = 512,
) -> ExclusionResult:
    """Count (and, when paths are given, split) the source by the rules. With no rules nothing is written and every row is included."""
    with bounded_connection(memory_mb) as con:
        _, relation = file_relation(source)
        total = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
        if not rules:
            return ExclusionResult(total_rows=total, included_rows=total, excluded_rows=0, rules=[])
        columns = _file_columns(con, relation)
        resolved = {r.rule_id: _resolve(columns, r.column) for r in rules}
        order = " ".join(f"WHEN {predicate(r, resolved[r.rule_id])} THEN {r.rule_id}" for r in rules)
        con.execute(f'CREATE TEMP TABLE tagged AS SELECT *, CASE {order} END AS "__excl" FROM {relation}')
        per_rule = dict(con.execute('SELECT "__excl", count(*) FROM tagged WHERE "__excl" IS NOT NULL GROUP BY 1').fetchall())
        excluded = sum(int(n) for n in per_rule.values())
        counts = []
        for r in rules:
            column = resolved[r.rule_id]
            examples = [str(v[0]) for v in con.execute(
                f'SELECT DISTINCT {_text(column)} FROM tagged WHERE "__excl" = {int(r.rule_id)} LIMIT {MAX_EXAMPLES}').fetchall()] if per_rule.get(r.rule_id) else []
            counts.append(ExclusionRuleCount(rule_id=r.rule_id, column=r.column, operator=r.operator, values=r.values, reason=r.reason,
                                             excluded=int(per_rule.get(r.rule_id, 0)), examples=examples))
        if excluded >= total:
            raise ExclusionError(f"the exclusion rules remove all {total:,} rows, so there would be nothing to migrate")
        if included_path is not None:
            included_path.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f'COPY (SELECT * EXCLUDE ("__excl") FROM tagged WHERE "__excl" IS NULL) TO {lit(included_path.as_posix())} (FORMAT parquet)')
        if excluded_path is not None and excluded:
            reasons = " ".join(f"WHEN {r.rule_id} THEN {lit(r.reason)}" for r in rules)
            excluded_path.parent.mkdir(parents=True, exist_ok=True)
            con.execute(
                f'COPY (SELECT * EXCLUDE ("__excl"), "__excl" AS {q(EXCLUDED_BY)}, CASE "__excl" {reasons} END AS {q(EXCLUDED_REASON)} '
                f'FROM tagged WHERE "__excl" IS NOT NULL) TO {lit(excluded_path.as_posix())} (FORMAT parquet)')
    return ExclusionResult(
        total_rows=total, included_rows=total - excluded, excluded_rows=excluded, rules=counts,
        included_path=str(included_path) if included_path else None, excluded_path=str(excluded_path) if excluded_path and excluded else None)


def preview_rule(source: Path, rule: ExclusionRuleInput) -> ExclusionRuleCount:
    """How many rows (and which values) a rule would remove, without saving it. Overlap with other rules is not considered."""
    column = validate_rule(source, rule)
    with bounded_connection(256) as con:
        _, relation = file_relation(source)
        where = predicate(rule, column)
        n = int(con.execute(f"SELECT count(*) FROM {relation} WHERE {where}").fetchone()[0])
        examples = [str(r[0]) for r in con.execute(f"SELECT DISTINCT {_text(column)} FROM {relation} WHERE {where} LIMIT {MAX_EXAMPLES}").fetchall()] if n else []
    return ExclusionRuleCount(rule_id=0, column=column, operator=rule.operator, values=rule.values, reason=rule.reason, excluded=n, examples=examples)


# ------------------------------------------------------------------ suggestions
def suggest(source: Path, memory_mb: int = 256) -> list[ExclusionSuggestion]:
    """Rules the data itself points to: a deletion-flag column that is set on some rows, and names that say TEST or DUMMY. Never applied
    automatically: a person looks at the match count and the examples and decides."""
    out: list[ExclusionSuggestion] = []
    with bounded_connection(memory_mb) as con:
        _, relation = file_relation(source)
        total = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
        for column in _file_columns(con, relation):
            if _FLAG_COLUMN.search(column):
                top = con.execute(
                    f"SELECT {_text(column)} AS v, count(*) AS n FROM {relation} WHERE {_text(column)} <> '' GROUP BY 1 ORDER BY n DESC LIMIT 1").fetchone()
                if top and top[0].upper() in _FLAG_VALUES and 0 < top[1] < total * 0.5:
                    out.append(ExclusionSuggestion(
                        column=column, operator="equals", values=[top[0]], matches=int(top[1]), examples=[top[0]],
                        reason=f"{column} = '{top[0]}' marks the record as deleted or inactive in the legacy system"))
            elif _NAME_COLUMN.search(column):
                where = f"regexp_matches({_text(column)}, {lit(_TEST_PATTERN)}, 'i')"
                n = int(con.execute(f"SELECT count(*) FROM {relation} WHERE {where}").fetchone()[0])
                if n:
                    ex = [str(r[0]) for r in con.execute(f"SELECT DISTINCT {_text(column)} FROM {relation} WHERE {where} LIMIT {MAX_EXAMPLES}").fetchall()]
                    out.append(ExclusionSuggestion(
                        column=column, operator="regex", values=[_TEST_PATTERN], matches=n, examples=ex,
                        reason=f"{column} looks like a test or dummy record (contains TEST, DUMMY, DO NOT USE or DELETE ME)"))
    return sorted(out, key=lambda s: -s.matches)


# ------------------------------------------------------------------ storage
class ExclusionStore:
    """The exclusion rules of every mapping set, one JSON file each under <workspace>/exclusions/."""

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace) / "exclusions"
        self._lock = threading.RLock()  # readers take it too: on Windows a file that is being read cannot be replaced, and a failed read must not look like 'no rules'

    def _path(self, mapping_set_id: int) -> Path:
        return self._dir / f"mapping_{int(mapping_set_id)}.json"

    def rules(self, mapping_set_id: int) -> list[ExclusionRule]:
        with self._lock:
            try:
                return [ExclusionRule.model_validate(r) for r in json.loads(self._path(mapping_set_id).read_text(encoding="utf-8"))]
            except (OSError, ValueError):
                return []

    def _save(self, mapping_set_id: int, rules: list[ExclusionRule]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(mapping_set_id).with_suffix(".tmp")
        tmp.write_text(json.dumps([r.model_dump() for r in rules], indent=1), encoding="utf-8")
        tmp.replace(self._path(mapping_set_id))

    def add(self, mapping_set_id: int, rule: ExclusionRuleInput, column: str, user: str) -> ExclusionRule:
        with self._lock:
            rules = self.rules(mapping_set_id)
            created = ExclusionRule(
                rule_id=max((r.rule_id for r in rules), default=0) + 1, column=column, operator=rule.operator, values=rule.values,
                reason=rule.reason, ignore_case=rule.ignore_case, created_by=user, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._save(mapping_set_id, [*rules, created])
            return created

    def delete(self, mapping_set_id: int, rule_id: int) -> Optional[ExclusionRule]:
        with self._lock:
            rules = self.rules(mapping_set_id)
            gone = next((r for r in rules if r.rule_id == rule_id), None)
            if gone:
                self._save(mapping_set_id, [r for r in rules if r.rule_id != rule_id])
            return gone

    @staticmethod
    def write_applied(run_dir: Path, result: ExclusionResult) -> None:
        """What a run actually left out, next to the run's other evidence (the reconciliation certificate reads it)."""
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / APPLIED_FILE).write_text(result.model_dump_json(indent=1), encoding="utf-8")

    @staticmethod
    def read_applied(run_dir: Path) -> Optional[ExclusionResult]:
        try:
            return ExclusionResult.model_validate_json((run_dir / APPLIED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
