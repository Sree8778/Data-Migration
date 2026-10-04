"""Defect aggregation: writes validation failures to `gov_defect_aggregates`.

One row per (run, field rule, rule signature) - the table's unique constraint - holding the
violation count and up to 5 sample failing source keys. Recording is idempotent: re-validating a
run updates the existing rows (Jira linkage is preserved) and sets signatures that no longer fail
to zero violations instead of deleting them, so the audit trail stays intact.

A defect is *open* while its violation_count > 0, regardless of the Jira ticket state: the data
is the truth, a closed ticket does not make bad records good.

Recording also advances the run to phase VALIDATE, which the governance gate uses as evidence
that validation actually happened.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import MigrationPhase
from src.db.models import GovDefectAggregate, MapFieldRule, MigMigrationRun
from src.schemas.validation import (
    DefectRead,
    DefectRecordSummary,
    Severity,
    ValidationReport,
    severity_for_signature,
)
from src.services.metadata_service import NotFoundError

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
_PRE_VALIDATE_PHASES = (MigrationPhase.EXTRACT, MigrationPhase.PROFILE, MigrationPhase.TRANSFORM)


class DefectService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ write
    def record_defects(self, run_id: int, report: ValidationReport) -> DefectRecordSummary:
        """Upsert the report's failures for `run_id` (all in one transaction)."""
        with self._session_factory.begin() as session:
            run = session.get(MigMigrationRun, run_id)
            if run is None:
                raise NotFoundError(f"migration run {run_id} does not exist")
            if report.mapping_set_id != run.mapping_set_id:
                raise ValueError(
                    f"report is for mapping set {report.mapping_set_id}, run {run_id} uses {run.mapping_set_id}"
                )
            valid_rules = set(session.scalars(
                select(MapFieldRule.id).where(MapFieldRule.mapping_set_id == run.mapping_set_id)))
            unknown = sorted({f.field_rule_id for f in report.failures} - valid_rules)
            if unknown:
                raise ValueError(f"failures reference field rules outside the run's mapping set: {unknown}")

            existing = {
                (d.field_rule_id, d.rule_signature): d
                for d in session.scalars(select(GovDefectAggregate).where(GovDefectAggregate.run_id == run_id))
            }
            current = {(f.field_rule_id, f.signature) for f in report.failures}
            created = sum(1 for key in current if key not in existing)
            updated = len(current) - created

            for f in report.failures:
                stmt = pg_insert(GovDefectAggregate).values(
                    run_id=run_id, field_rule_id=f.field_rule_id, rule_signature=f.signature,
                    violation_count=f.violation_count, sample_failing_record_ids=f.sample_failing_pks,
                )
                session.execute(stmt.on_conflict_do_update(
                    constraint="uq_gov_defect_aggregates_run_rule_sig",
                    set_={
                        "violation_count": stmt.excluded.violation_count,
                        "sample_failing_record_ids": stmt.excluded.sample_failing_record_ids,
                        "updated_at": func.now(),
                    },
                ))

            resolved = 0
            for key, row in existing.items():
                if key not in current and row.violation_count > 0:
                    row.violation_count = 0
                    row.sample_failing_record_ids = []
                    resolved += 1

            if run.current_phase in _PRE_VALIDATE_PHASES:
                run.current_phase = MigrationPhase.VALIDATE
            session.flush()
            total_open = session.scalar(
                select(func.count()).select_from(GovDefectAggregate).where(
                    GovDefectAggregate.run_id == run_id, GovDefectAggregate.violation_count > 0)
            )
            return DefectRecordSummary(
                run_id=run_id, created=created, updated=updated, resolved=resolved, total_open=int(total_open))

    def set_jira_link(self, defect_id: int, issue_key: str, status: str) -> None:
        with self._session_factory.begin() as session:
            session.execute(update(GovDefectAggregate).where(GovDefectAggregate.id == defect_id).values(
                jira_issue_key=issue_key, jira_status=status, updated_at=func.now()))

    # ------------------------------------------------------------------ read
    def get_defects(self, run_id: int, open_only: bool = False) -> list[DefectRead]:
        with self._session_factory() as session:
            query = select(GovDefectAggregate).where(GovDefectAggregate.run_id == run_id)
            if open_only:
                query = query.where(GovDefectAggregate.violation_count > 0)
            rows = session.scalars(query).all()
            defects = [self._to_read(r) for r in rows]
        return sorted(defects, key=lambda d: (_SEVERITY_ORDER[d.severity], -d.violation_count, d.rule_signature))

    def count_open(self, run_id: int, severity: Severity | None = None) -> int:
        defects = self.get_defects(run_id, open_only=True)
        return sum(1 for d in defects if severity is None or d.severity == severity)

    @staticmethod
    def _to_read(row: GovDefectAggregate) -> DefectRead:
        return DefectRead(
            id=row.id, run_id=row.run_id, field_rule_id=row.field_rule_id, rule_signature=row.rule_signature,
            severity=severity_for_signature(row.rule_signature), violation_count=row.violation_count,
            sample_failing_record_ids=[str(x) for x in (row.sample_failing_record_ids or [])],
            jira_issue_key=row.jira_issue_key, jira_status=row.jira_status, is_open=row.violation_count > 0,
        )
