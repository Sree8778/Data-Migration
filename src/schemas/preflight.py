"""Result of the pre-submission check of a mapping set: static rule checks plus a dry run of the mapping on a sample of the real rows."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FindingSeverity(str, enum.Enum):
    BLOCKER = "BLOCKER"  # the mapping set cannot be submitted for approval until this is fixed
    WARNING = "WARNING"  # shown to the submitter and the approver, does not stop anything


class PreflightFinding(BaseModel):
    severity: FindingSeverity
    code: str  # NO_RULES, EMPTY_LOOKUP, SOURCE_MISSING, MOSTLY_FAILING, LOW_YIELD, PARTIAL_FAILING, MANDATORY_UNMAPPED, UNPINNED_MODEL, REVIEW_PENDING, NOT_SAMPLED
    message: str
    rule_id: Optional[int] = None
    target: Optional[str] = None


class UnmatchedValue(BaseModel):
    value: str
    count: int


class RuleCheck(BaseModel):
    rule_id: int
    target: str  # TABLE.COLUMN
    source_column: Optional[str]
    rule_type: str
    lookup_count: int = 0
    sampled: int = 0  # rows the rule was evaluated on
    failed: int = 0  # of those, rows where this rule's value failed validation
    pass_rate: Optional[float] = None  # None when no sample could be run
    failure_kinds: dict[str, int] = Field(default_factory=dict)  # LOOKUP_FAILED / MANDATORY / LENGTH / CONVERSION / CHECKTABLE -> rows
    unmatched_values: list[UnmatchedValue] = Field(default_factory=list)  # legacy values a lookup rule has no entry for, most frequent first
    verdict: str = "OK"  # OK | WARNING | BLOCKER


class PreflightReport(BaseModel):
    mapping_set_id: int
    checked_at: datetime
    sampled: bool  # False when only the static checks could run (no landed source file)
    sample_requested: int
    sample_rows: int = 0
    source_rows: Optional[int] = None
    valid_rate: Optional[float] = None  # share of sampled rows that pass every rule
    block_rate: float
    warn_rate: float
    can_submit: bool
    blockers: int
    warnings: int
    findings: list[PreflightFinding]
    rules: list[RuleCheck]
    models: list[str] = Field(default_factory=list)  # the AI model names recorded on the mapping's rules
    exclusion_rules: list[int] = Field(default_factory=list)  # ids of the exclusion rules the sample was cut by
