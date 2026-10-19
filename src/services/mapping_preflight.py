"""Pre-submission check of a mapping set: does it actually work on this data?

An AI proposal can look right and still be wrong in a way only the data shows: a lookup rule with an empty table rejects every row, a
"copy" of a column that holds country names into a field that holds ISO codes fails a third of them. This service finds that BEFORE the
mapping is handed to an approver, with two kinds of check:

  * static, no data needed:   rule structure (no rules, a VALUE_MAPPING with no lookups, a source column that is not in the landed file),
                              mandatory SAP fields nothing maps to, rules still waiting for a human review, a model that is an alias
  * dry run on real rows:     a reproducible sample of the landed file goes through the real transformation and the real validation (the
                              same engines the pipeline uses, writing nothing to the run tables), and every rule gets a measured pass rate;
                              for lookup rules, the legacy values that have no entry are listed so the steward can fill them in

A rule that fails most of the sample, or a lookup rule with nothing in its table, is a BLOCKER: the mapping set cannot be submitted.
A rule that fails a visible share is a WARNING that the submitter and the approver both see. Thresholds are parameters.
"""

from __future__ import annotations

import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import RuleType
from src.db.models import CatColumn, CatTable, MapFieldRule, MapTableRule
from src.schemas.execution import LineageMode
from src.schemas.preflight import FindingSeverity, PreflightFinding, PreflightReport, RuleCheck, UnmatchedValue
from src.services._duckdb_util import bounded_connection, file_relation, lit, q
from src.services.lookup_service import LookupService
from src.services.metadata_service import NotFoundError
from src.services.transformation_engine import ERRORS_COLUMN, TransformationEngine
from src.services.validation_engine import ValidationEngine

DEFAULT_SAMPLE_ROWS = 2000
DEFAULT_BLOCK_RATE = 0.5  # a rule failing at least this share of the sample blocks submission
DEFAULT_WARN_RATE = 0.05  # ... and at least this share raises a warning
SAMPLE_SEED = 42  # the same rows every time, so a re-check after an edit compares like with like
MAX_UNMATCHED = 8
_NEEDS_REVIEW = re.compile(r"^\[NEEDS_REVIEW\]")
_ALIAS_MODEL = re.compile(r"(^|[-:])latest\b")


class MappingPreflightService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        transformer: TransformationEngine,
        validator: ValidationEngine,
        lookups: Optional[LookupService] = None,
        memory_limit_mb: int = 512,
    ) -> None:
        self._sf = session_factory
        self._transformer = transformer
        self._validator = validator
        self._lookups = lookups or LookupService(session_factory)
        self._memory = memory_limit_mb

    # ------------------------------------------------------------------ public API
    def check(
        self,
        mapping_set_id: int,
        source_path: Optional[Path] = None,
        *,
        sample_rows: int = DEFAULT_SAMPLE_ROWS,
        block_rate: float = DEFAULT_BLOCK_RATE,
        warn_rate: float = DEFAULT_WARN_RATE,
        file_columns: Optional[set[str]] = None,
        unmapped_mandatory: Optional[list[str]] = None,
    ) -> PreflightReport:
        """`file_columns`: lower-cased columns of the landed file, to catch rules that read a column the file no longer has."""
        rules = self._load_rules(mapping_set_id)
        findings: list[PreflightFinding] = []
        checks: dict[int, RuleCheck] = {
            r["rule_id"]: RuleCheck(rule_id=r["rule_id"], target=r["target"], source_column=r["source"], rule_type=r["rule_type"], lookup_count=r["lookups"])
            for r in rules
        }

        def add(severity: FindingSeverity, code: str, message: str, rule: Optional[dict] = None) -> None:
            findings.append(PreflightFinding(severity=severity, code=code, message=message,
                                             rule_id=rule["rule_id"] if rule else None, target=rule["target"] if rule else None))
            if rule and severity == FindingSeverity.BLOCKER:
                checks[rule["rule_id"]].verdict = "BLOCKER"
            elif rule and checks[rule["rule_id"]].verdict == "OK":
                checks[rule["rule_id"]].verdict = "WARNING"

        # ---- static checks
        if not rules:
            add(FindingSeverity.BLOCKER, "NO_RULES", "the mapping set has no rules")
        runnable = bool(rules)
        for r in rules:
            if r["rule_type"] == RuleType.VALUE_MAPPING.value and r["lookups"] == 0:
                add(FindingSeverity.BLOCKER, "EMPTY_LOOKUP",
                    f"{r['target']} is a value mapping with an empty lookup table, so every row would be rejected: add the translations or choose another rule type", r)
            if file_columns is not None and r["source"] and r["source"].lower() not in file_columns:
                add(FindingSeverity.BLOCKER, "SOURCE_MISSING", f"{r['target']} reads column {r['source']}, which is not in the landed file", r)
                runnable = False
        pending = [r for r in rules if r["needs_review"]]
        if pending:
            add(FindingSeverity.WARNING, "REVIEW_PENDING",
                f"{len(pending)} rule(s) are flagged for review and nobody has confirmed them yet: " + ", ".join(r["target"] for r in pending[:6]) + ("..." if len(pending) > 6 else ""))
        if unmapped_mandatory:
            add(FindingSeverity.WARNING, "MANDATORY_UNMAPPED",
                f"{len(unmapped_mandatory)} mandatory SAP field(s) have no rule and will be empty (Data Construction can supply them): "
                + ", ".join(unmapped_mandatory[:6]) + ("..." if len(unmapped_mandatory) > 6 else ""))
        models = sorted({r["model"] for r in rules if r["model"]})
        alias = [m for m in models if _ALIAS_MODEL.search(m.split(" -> ")[0])]
        if alias:
            add(FindingSeverity.WARNING, "UNPINNED_MODEL",
                f"the mapping came from the model alias '{alias[0].split(' -> ')[0]}', which its vendor can repoint at a different model; "
                "set GEMINI_MODEL / OLLAMA_MODEL to a versioned name so a re-run gives the same mapping")

        # ---- dry run on a sample of the real rows
        sampled, sample_n, source_rows, valid_rate = False, 0, None, None
        if source_path is None or not Path(source_path).is_file():
            add(FindingSeverity.WARNING, "NOT_SAMPLED", "no source file is landed for this table, so the rules could only be checked structurally")
        elif runnable and rules:
            try:
                sample_n, source_rows, valid_rate = self._dry_run(mapping_set_id, Path(source_path), rules, checks, sample_rows)
                sampled = True
            except Exception as exc:  # noqa: BLE001 - a rule the engine cannot even compile is itself the finding
                add(FindingSeverity.BLOCKER, "DRY_RUN_FAILED", f"the mapping could not be run on a sample: {str(exc)[:300]}")
        if sampled:
            for r in rules:
                c = checks[r["rule_id"]]
                if c.sampled and c.failed:
                    rate = c.failed / c.sampled
                    if r["placeholder"]:
                        add(FindingSeverity.WARNING, "AWAITING_CONSTRUCTION",
                            f"{r['target']} is a placeholder: {rate:.0%} of the sampled rows still need a value from Data Construction", r)
                    elif rate >= block_rate:
                        add(FindingSeverity.BLOCKER, "MOSTLY_FAILING",
                            f"{r['target']} fails {rate:.0%} of the {c.sampled:,} sampled rows ({_kinds(c)}): fix the rule before submitting", r)
                    elif rate >= warn_rate:
                        add(FindingSeverity.WARNING, "PARTIAL_FAILING", f"{r['target']} fails {rate:.0%} of the {c.sampled:,} sampled rows ({_kinds(c)})", r)
            if valid_rate is not None and valid_rate < block_rate:
                waiting = any(r["placeholder"] for r in rules)  # placeholder rules fail by design, so a low yield is expected until they are filled
                add(FindingSeverity.WARNING if waiting else FindingSeverity.BLOCKER, "LOW_YIELD",
                    f"only {valid_rate:.0%} of the sampled rows pass validation" + (" (placeholder rules are still empty)" if waiting else ""))

        blockers = sum(1 for f in findings if f.severity == FindingSeverity.BLOCKER)
        return PreflightReport(
            mapping_set_id=mapping_set_id, checked_at=datetime.now(timezone.utc), sampled=sampled, sample_requested=sample_rows,
            sample_rows=sample_n, source_rows=source_rows, valid_rate=valid_rate, block_rate=block_rate, warn_rate=warn_rate,
            can_submit=blockers == 0, blockers=blockers, warnings=len(findings) - blockers,
            findings=sorted(findings, key=lambda f: (f.severity != FindingSeverity.BLOCKER, f.code)),
            rules=list(checks.values()), models=models)

    # ------------------------------------------------------------------ the rules
    def _load_rules(self, mapping_set_id: int) -> list[dict]:
        with self._sf() as s:
            if s.get(MapTableRule, mapping_set_id) is None:
                raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
            out = []
            for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id).order_by(MapFieldRule.id)):
                target = s.get(CatColumn, r.target_column_id)
                table = s.get(CatTable, target.table_id)
                source = s.get(CatColumn, r.source_column_id) if r.source_column_id else None
                reviewed = bool(r.ai_reasoning and "[REVIEWED by " in r.ai_reasoning)
                out.append({
                    "rule_id": r.id, "target": f"{table.table_name}.{target.column_name}", "staging": f"{table.table_name}_{target.column_name}",
                    "source": source.column_name if source else None, "rule_type": r.rule_type.value, "model": r.ai_model_name,
                    "needs_review": bool(r.ai_reasoning and _NEEDS_REVIEW.match(r.ai_reasoning)) and not reviewed, "lookups": 0,
                    # a placeholder stands in for a mandatory field the legacy system never had: it fails validation by design until
                    # Data Construction supplies the values, so it is reported but never blocks
                    "placeholder": bool(r.ai_reasoning and r.ai_reasoning.startswith("Construction placeholder")),
                })
        for r in out:
            if r["rule_type"] == RuleType.VALUE_MAPPING.value:
                r["lookups"] = len(self._lookups.get_lookups(r["rule_id"]))
        return out

    # ------------------------------------------------------------------ the dry run
    def _dry_run(self, mapping_set_id: int, source: Path, rules: list[dict], checks: dict[int, RuleCheck], sample_rows: int) -> tuple[int, int, float]:
        lookup_sources = list(dict.fromkeys(r["source"] for r in rules if r["rule_type"] == RuleType.VALUE_MAPPING.value and r["source"]))
        with tempfile.TemporaryDirectory(prefix="preflight_") as tmp:
            sample = Path(tmp) / "sample.parquet"
            _, relation = file_relation(source)
            with bounded_connection(self._memory) as con:
                total = int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])
                take = f"SELECT * FROM {relation}" + (f" USING SAMPLE reservoir({int(sample_rows)} ROWS) REPEATABLE ({SAMPLE_SEED})" if total > sample_rows else "")
                con.execute(f"COPY ({take}) TO {lit(sample.as_posix())} (FORMAT parquet)")
            staging = Path(tmp) / "staging.parquet"
            self._transformer.transform(mapping_set_id, sample, staging, lineage_mode=LineageMode.NONE, passthrough_columns=lookup_sources or None)
            report = self._validator.validate(mapping_set_id, staging, None)
            evaluated = report.evaluated_records
            kinds: dict[int, dict[str, int]] = defaultdict(dict)
            for f in report.failures:
                kinds[f.field_rule_id][f.kind.value] = kinds[f.field_rule_id].get(f.kind.value, 0) + f.violation_count
            for r in rules:
                c = checks[r["rule_id"]]
                c.sampled = evaluated
                c.failure_kinds = kinds.get(r["rule_id"], {})
                c.failed = min(evaluated, sum(c.failure_kinds.values()))  # one kind per row and rule, so the kinds add up
                c.pass_rate = round(1 - c.failed / evaluated, 4) if evaluated else None
                if c.failure_kinds.get("LOOKUP_FAILED") and r["source"]:
                    c.unmatched_values = self._unmatched(staging, r)
            valid_rate = report.valid_records / evaluated if evaluated else 0.0
            return evaluated, total, round(valid_rate, 4)

    def _unmatched(self, staging: Path, rule: dict) -> list[UnmatchedValue]:
        """The legacy values of a lookup rule that the lookup table has no entry for, most frequent first."""
        column = f"SRC_{rule['source']}"
        tag = f"{rule['target']}: LOOKUP_FAILED"
        try:
            with bounded_connection(self._memory) as con:
                names = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(staging.as_posix())})").fetchall()}
                if column not in names:
                    return []
                has_errors = ERRORS_COLUMN in names
                cond = f"{q(rule['staging'])} = 'LOOKUP_FAILED'" + (f" OR contains(coalesce({q(ERRORS_COLUMN)}, ''), {lit(tag)})" if has_errors else "")
                rows = con.execute(
                    f"SELECT coalesce(nullif(trim({q(column)}), ''), '(blank)'), count(*) AS n FROM read_parquet({lit(staging.as_posix())}) "
                    f"WHERE {cond} GROUP BY 1 ORDER BY n DESC, 1 LIMIT {MAX_UNMATCHED}").fetchall()
            return [UnmatchedValue(value=str(v), count=int(n)) for v, n in rows]
        except Exception:  # noqa: BLE001 - the list is a convenience; the pass rate is the finding
            return []


def _kinds(c: RuleCheck) -> str:
    return ", ".join(f"{k.lower().replace('_', ' ')} {n:,}" for k, n in sorted(c.failure_kinds.items(), key=lambda kv: -kv[1]))
