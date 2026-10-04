"""Pydantic v2 models and the deterministic signature/severity rules for Module 5."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ failure kinds / severity
class FailureKind(str, enum.Enum):
    LOOKUP = "LOOKUP_FAILED"  # translation had no mapping (or value is the LOOKUP_FAILED sentinel)
    MANDATORY = "MANDATORY"  # mandatory target is NULL / empty / whitespace
    LENGTH = "LENGTH"  # string longer than the target's char_max_length (truncation risk)
    CONVERSION = "CONVERSION"  # value could not be converted to the target data type
    CHECKTABLE = "CHECKTABLE"  # value is not in the target's check-table domain


class Severity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# Deterministic map. Mandatory-field violations and unresolved lookups are CRITICAL: the record
# cannot be created in SAP. Everything else is a HIGH-priority defect that does not block the gate.
SEVERITY_BY_KIND: dict[FailureKind, Severity] = {
    FailureKind.LOOKUP: Severity.CRITICAL,
    FailureKind.MANDATORY: Severity.CRITICAL,
    FailureKind.LENGTH: Severity.HIGH,
    FailureKind.CONVERSION: Severity.HIGH,
    FailureKind.CHECKTABLE: Severity.HIGH,
}

ROOT_CAUSE_BY_KIND: dict[FailureKind, str] = {
    FailureKind.LOOKUP: (
        "The legacy value has no entry in the value-lookup table for this rule. Add the missing "
        "translation(s) to the lookup or correct the source data."
    ),
    FailureKind.MANDATORY: (
        "The target field is mandatory in SAP but the transformed value is empty. Either the legacy "
        "field is blank or the mapping rule does not populate it; fix the source data or add a default."
    ),
    FailureKind.LENGTH: (
        "The transformed value is longer than the SAP field allows. Loading would truncate or reject it; "
        "shorten the value in the source or adjust the transformation."
    ),
    FailureKind.CONVERSION: (
        "The value cannot be converted to the target data type. Check the source format and the rule."
    ),
    FailureKind.CHECKTABLE: (
        "The value is not a valid key of the SAP check table for this field, so SAP would reject it. "
        "Map the legacy value to a valid key."
    ),
}

_SIGNATURE_PREFIX = "ERR_"


def signature_for(kind: FailureKind, column: str, table: Optional[str] = None) -> str:
    """ERR_<KIND>_<COLUMN>, or ERR_<KIND>_<TABLE>_<COLUMN> when the column name is ambiguous."""
    body = f"{table}_{column}" if table else column
    return f"{_SIGNATURE_PREFIX}{kind.value}_{body}".upper()


def kind_for_signature(signature: str) -> Optional[FailureKind]:
    body = signature.upper()
    if not body.startswith(_SIGNATURE_PREFIX):
        return None
    rest = body[len(_SIGNATURE_PREFIX):]
    # longest value first: LOOKUP_FAILED must win over a hypothetical shorter prefix
    for kind in sorted(FailureKind, key=lambda k: -len(k.value)):
        if rest.startswith(kind.value + "_"):
            return kind
    return None


def severity_for_signature(signature: str) -> Severity:
    """Deterministic severity for any stored signature (unknown signatures are HIGH, never silent)."""
    kind = kind_for_signature(signature)
    return SEVERITY_BY_KIND[kind] if kind else Severity.HIGH


# ------------------------------------------------------------------ validation report
class FailureBreakdown(_Model):
    signature: str
    kind: FailureKind
    severity: Severity
    target: str  # TABLE.COLUMN
    field_rule_id: int
    violation_count: int = Field(ge=1)
    sample_failing_pks: list[str]


class ValidationReport(_Model):
    run_id: Optional[int] = None
    mapping_set_id: int
    staging_path: str
    input_records: int
    duplicate_children_excluded: int
    evaluated_records: int
    valid_records: int
    defective_records: int
    quality_score: float  # valid / evaluated, in percent
    failures: list[FailureBreakdown]
    checked_domains: list[str] = Field(default_factory=list)
    unchecked_check_tables: list[str] = Field(default_factory=list)  # check table has no known domain
    valid_path: Optional[str] = None
    rejected_path: Optional[str] = None
    duplicates_path: Optional[str] = None
    validated_at: datetime

    @property
    def critical_failures(self) -> list[FailureBreakdown]:
        return [f for f in self.failures if f.severity == Severity.CRITICAL]


# ------------------------------------------------------------------ defects
class DefectRead(_Model):
    id: int
    run_id: int
    field_rule_id: int
    rule_signature: str
    severity: Severity
    violation_count: int
    sample_failing_record_ids: list[str]
    jira_issue_key: Optional[str]
    jira_status: Optional[str]
    is_open: bool


class DefectRecordSummary(_Model):
    run_id: int
    created: int
    updated: int
    resolved: int  # previously failing signatures that now have zero violations
    total_open: int


# ------------------------------------------------------------------ governance
class GateState(str, enum.Enum):
    READY_FOR_LOAD = "READY_FOR_LOAD"
    BLOCKED = "BLOCKED"


class ReadinessResult(_Model):
    run_id: int
    mapping_set_id: int
    state: GateState
    mapping_status: str
    run_status: str
    validated: bool
    open_critical_defects: int
    open_defects: int
    reasons: list[str]

    @property
    def ready(self) -> bool:
        return self.state == GateState.READY_FOR_LOAD
