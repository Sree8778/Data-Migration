"""Pydantic v2 contracts for the semantic mapping engine (Module 3)."""

from __future__ import annotations

import enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldMappingRecommendation(BaseModel):
    """The strict contract the LLM must satisfy for each source column."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_table: str = Field(min_length=1)
    target_column: str = Field(min_length=1)
    confidence_score: float = Field(ge=0.0, le=1.0)
    rule_type: Literal["DIRECT_COPY", "VALUE_MAPPING", "SQL_EXPRESSION", "STATIC_VALUE"]
    transformation_sql: Optional[str] = None  # e.g. LPAD(TRIM(source_col), 10, '0')
    reasoning: str = Field(min_length=1)

    @field_validator("transformation_sql")
    @classmethod
    def _blank_is_none(cls, value: Optional[str]) -> Optional[str]:
        return value if value and value.strip() else None


class TargetCandidate(BaseModel):
    """A retrieved SAP target column with its cosine similarity to the source column."""

    model_config = ConfigDict(frozen=True)

    column_id: int
    table_id: int
    table_name: str
    column_name: str
    data_type: str
    char_max_length: Optional[int] = None
    business_name: Optional[str] = None  # business name of the owning table
    description: Optional[str] = None
    check_table: Optional[str] = None
    is_mandatory: bool = False
    score: float = Field(ge=-1.0, le=1.0)


class SourceColumnContext(BaseModel):
    """Everything the LLM is allowed to see about a source column."""

    column_id: int
    column_name: str
    data_type: str
    char_max_length: Optional[int] = None
    null_ratio: Optional[float] = None
    distinct_ratio: Optional[float] = None
    sample_values: list[str] = Field(default_factory=list)  # already masked, max 3
    table_name: str = ""


class ReviewStatus(str, enum.Enum):
    OK = "OK"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ValidationResult(BaseModel):
    status: ReviewStatus
    # True when the recommendation can be stored in map_field_rules (target resolved and,
    # for SQL rule types, a safe expression is present).
    persistable: bool
    target_table_id: Optional[int] = None
    target_column_id: Optional[int] = None
    target_is_mandatory: bool = False
    transformation_sql: Optional[str] = None  # sanitised; None if rejected
    reasons: list[str] = Field(default_factory=list)


class ProposalOutcome(str, enum.Enum):
    SAVED = "SAVED"
    NO_MATCH = "NO_MATCH"
    REJECTED = "REJECTED"  # failed guardrails in a way that cannot be stored
    SUPERSEDED = "SUPERSEDED"  # another source column won the same target column
    ERROR = "ERROR"  # LLM output unusable after retry


class ColumnProposal(BaseModel):
    source_column: str
    outcome: ProposalOutcome
    status: Optional[ReviewStatus] = None
    recommendation: Optional[FieldMappingRecommendation] = None
    reasons: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)  # "TABLE.COLUMN" retrieved
    field_rule_id: Optional[int] = None


class MappingGenerationReport(BaseModel):
    mapping_set_id: int
    mapping_set_name: str
    source_table_id: int
    target_table_id: int
    version: int
    model_name: str
    proposals: list[ColumnProposal]
    skipped_unprofiled_columns: list[str] = Field(default_factory=list)
    unmapped_mandatory_targets: list[str] = Field(default_factory=list)

    def by_outcome(self, outcome: ProposalOutcome) -> list[ColumnProposal]:
        return [p for p in self.proposals if p.outcome == outcome]

    def model_summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for p in self.proposals:
            counts[p.outcome.value] = counts.get(p.outcome.value, 0) + 1
        needs_review = sum(1 for p in self.proposals if p.status == ReviewStatus.NEEDS_REVIEW and p.outcome == ProposalOutcome.SAVED)
        return {"outcomes": counts, "saved_needs_review": needs_review}
