"""Governance approval state machine and the READY_FOR_LOAD gate.

Mapping-set lifecycle (existing `MappingStatus` enum, unchanged):

    DRAFT -> SUBMITTED -> APPROVED          (APPROVED is terminal: change = new mapping version)
                       -> REJECTED -> DRAFT

Segregation of duties: the approver must differ from `created_by` (compared trimmed and
case-insensitively here; the database CHECK `maker_checker_separation` is the exact-match backstop).
`map_table_rules` records only the creator, so "last modifier" cannot be enforced beyond that.
An optional allow-list of authorised stewards can restrict who may approve.

READY_FOR_LOAD is a *computed* result, never stored. A run is ready only when
  * its mapping set is APPROVED,
  * the run SUCCEEDED and validation was recorded (phase VALIDATE or later), and
  * no CRITICAL defect has violations left.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import MappingStatus, MigrationPhase, RunStatus
from src.db.models import MapTableRule, MigMigrationRun
from src.schemas.metadata import MappingSetRead
from src.schemas.validation import GateState, ReadinessResult, Severity
from src.services.defect_service import DefectService
from src.services.metadata_service import NotFoundError

ALLOWED_TRANSITIONS: dict[MappingStatus, set[MappingStatus]] = {
    MappingStatus.DRAFT: {MappingStatus.SUBMITTED},
    MappingStatus.SUBMITTED: {MappingStatus.APPROVED, MappingStatus.REJECTED},
    MappingStatus.REJECTED: {MappingStatus.DRAFT},
    MappingStatus.APPROVED: set(),
}
_VALIDATED_PHASES = (MigrationPhase.VALIDATE, MigrationPhase.LOAD, MigrationPhase.RECONCILE, MigrationPhase.COMPLETE)


class GovernanceError(Exception):
    pass


class InvalidTransitionError(GovernanceError):
    pass


class SegregationOfDutiesError(GovernanceError):
    pass


class NotAuthorizedError(GovernanceError):
    pass


class GateBlockedError(GovernanceError):
    def __init__(self, result: ReadinessResult) -> None:
        super().__init__("not ready for load: " + "; ".join(result.reasons))
        self.result = result


def _same_user(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


class GovernanceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        defect_service: Optional[DefectService] = None,
        authorized_approvers: Optional[Iterable[str]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._defects = defect_service or DefectService(session_factory)
        self._approvers = (
            {a.strip().casefold() for a in authorized_approvers} if authorized_approvers is not None else None
        )

    # ------------------------------------------------------------------ transitions
    def submit_mapping_set(self, mapping_set_id: int, submitted_by: str) -> MappingSetRead:
        self._require_user(submitted_by, "submitted_by")
        return self._transition(mapping_set_id, MappingStatus.SUBMITTED)

    def approve_mapping_set(
        self, mapping_set_id: int, approved_by: str, approved_at: Optional[datetime] = None
    ) -> MappingSetRead:
        """SUBMITTED -> APPROVED, recording approver and timestamp (UTC)."""
        self._require_user(approved_by, "approved_by")
        approver = approved_by.strip()
        if self._approvers is not None and approver.casefold() not in self._approvers:
            raise NotAuthorizedError(f"'{approver}' is not an authorised approver")
        try:
            with self._session_factory.begin() as session:
                mapping = self._lock(session, mapping_set_id)
                self._check_transition(mapping, MappingStatus.APPROVED)
                if _same_user(approver, mapping.created_by):
                    raise SegregationOfDutiesError(
                        f"segregation of duties: '{approver}' created mapping set {mapping_set_id} "
                        "and cannot approve it"
                    )
                mapping.status = MappingStatus.APPROVED
                mapping.approved_by = approver
                mapping.approved_at = approved_at or datetime.now(timezone.utc)
                session.flush()
                session.refresh(mapping)
                return MappingSetRead.model_validate(mapping)
        except IntegrityError as exc:  # the database CHECK constraints are the last line of defence
            raise SegregationOfDutiesError(f"approval rejected by database constraint: {exc.orig}") from exc

    def reject_mapping_set(self, mapping_set_id: int, rejected_by: str) -> MappingSetRead:
        """SUBMITTED -> REJECTED (the reviewer cannot be the creator). The schema has no rejector column."""
        self._require_user(rejected_by, "rejected_by")
        return self._transition(mapping_set_id, MappingStatus.REJECTED, reviewer=rejected_by)

    def return_to_draft(self, mapping_set_id: int, user: str) -> MappingSetRead:
        """REJECTED -> DRAFT for rework."""
        self._require_user(user, "user")
        return self._transition(mapping_set_id, MappingStatus.DRAFT)

    # ------------------------------------------------------------------ gate
    def evaluate_readiness(self, run_id: int) -> ReadinessResult:
        """Compute (never store) whether the run's dataset may be released for load."""
        with self._session_factory() as session:
            run = session.get(MigMigrationRun, run_id)
            if run is None:
                raise NotFoundError(f"migration run {run_id} does not exist")
            mapping = session.get(MapTableRule, run.mapping_set_id)
            mapping_status, run_status, phase = mapping.status, run.status, run.current_phase
            mapping_set_id = run.mapping_set_id

        reasons: list[str] = []
        if mapping_status != MappingStatus.APPROVED:
            reasons.append(f"mapping set {mapping_set_id} is {mapping_status.value}; it must be APPROVED")
        if run_status != RunStatus.SUCCEEDED:
            reasons.append(f"run {run_id} status is {run_status.value}; it must be SUCCEEDED")
        validated = phase in _VALIDATED_PHASES
        if not validated:
            reasons.append(f"validation has not been recorded for run {run_id}")

        open_defects = self._defects.get_defects(run_id, open_only=True)
        critical = [d for d in open_defects if d.severity == Severity.CRITICAL]
        if critical:
            listing = ", ".join(f"{d.rule_signature} ({d.violation_count})" for d in critical)
            reasons.append(f"{len(critical)} CRITICAL defect(s) unresolved: {listing}")

        return ReadinessResult(
            run_id=run_id, mapping_set_id=mapping_set_id,
            state=GateState.BLOCKED if reasons else GateState.READY_FOR_LOAD,
            mapping_status=mapping_status.value, run_status=run_status.value, validated=validated,
            open_critical_defects=len(critical), open_defects=len(open_defects), reasons=reasons,
        )

    def require_ready_for_load(self, run_id: int) -> ReadinessResult:
        """Return the READY_FOR_LOAD result or raise GateBlockedError (what a loader must call)."""
        result = self.evaluate_readiness(run_id)
        if not result.ready:
            raise GateBlockedError(result)
        return result

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _require_user(user: str, label: str) -> None:
        if not user or not user.strip():
            raise ValueError(f"{label} must not be empty")

    @staticmethod
    def _lock(session: Session, mapping_set_id: int) -> MapTableRule:
        mapping = session.scalar(
            select(MapTableRule).where(MapTableRule.id == mapping_set_id).with_for_update()
        )
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        return mapping

    @staticmethod
    def _check_transition(mapping: MapTableRule, target: MappingStatus) -> None:
        if target not in ALLOWED_TRANSITIONS[mapping.status]:
            raise InvalidTransitionError(
                f"mapping set {mapping.id}: cannot go from {mapping.status.value} to {target.value}"
            )

    def _transition(
        self, mapping_set_id: int, target: MappingStatus, reviewer: Optional[str] = None
    ) -> MappingSetRead:
        with self._session_factory.begin() as session:
            mapping = self._lock(session, mapping_set_id)
            self._check_transition(mapping, target)
            if reviewer is not None and _same_user(reviewer, mapping.created_by):
                raise SegregationOfDutiesError(
                    f"segregation of duties: '{reviewer.strip()}' created mapping set {mapping_set_id}"
                )
            mapping.status = target
            session.flush()
            session.refresh(mapping)
            return MappingSetRead.model_validate(mapping)
