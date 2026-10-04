"""Pydantic v2 models for Module 4: lookups, transformation, deduplication, run reporting."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ lookups
class LookupFallback(str, enum.Enum):
    PASS_THROUGH = "PASS_THROUGH"  # unmapped value is carried over unchanged
    FLAG_FAILED = "FLAG_FAILED"  # value becomes NULL and the cell is flagged LOOKUP_FAILED
    USE_DEFAULT = "USE_DEFAULT"  # unmapped value is replaced by a configured default


class LookupStatus(str, enum.Enum):
    MAPPED = "MAPPED"
    PASSED_THROUGH = "PASSED_THROUGH"
    DEFAULTED = "DEFAULTED"
    LOOKUP_FAILED = "LOOKUP_FAILED"
    NULL_INPUT = "NULL_INPUT"


class LookupResult(_Model):
    source_value: Optional[str]
    target_value: Optional[str]
    status: LookupStatus


class LookupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    field_rule_id: int
    source_value: str
    target_value: str


# ------------------------------------------------------------------ transformation
class LineageMode(str, enum.Enum):
    ALL = "ALL"  # every cell of every audited rule
    EXCEPTIONS = "EXCEPTIONS"  # only cells whose value changed or that failed
    NONE = "NONE"


class RuleSummary(_Model):
    rule_id: int
    rule_type: str
    source_column: Optional[str]
    target: str  # TABLE.COLUMN
    staging_column: str
    failed_cells: int = 0


class TransformationResult(_Model):
    mapping_set_id: int
    source_path: str
    output_path: str
    pk_strategy: Literal["CATALOG_PK", "PARAMETER", "ROW_NUMBER"]
    source_records: int
    ok_records: int
    failed_records: int
    rules: list[RuleSummary]
    lineage_rows: int = 0
    passthrough_columns: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ deduplication
class ExactKey(_Model):
    """A composite business key. Rows with identical, non-empty normalised values are duplicates."""

    name: str
    columns: list[str] = Field(min_length=1)
    # alnum: tax/VAT ids (strip punctuation, upper) | email: trim+lower | digits: phone numbers
    # text: collapse whitespace + upper
    normalizer: Literal["alnum", "email", "digits", "text"] = "text"


class DedupConfig(_Model):
    pk_column: str = "_SRC_PK"
    status_column: Optional[str] = "_TRANSFORM_STATUS"  # rows marked FAILED are not deduplicated
    exact_keys: list[ExactKey] = Field(default_factory=list)
    name_column: Optional[str] = None
    address_columns: list[str] = Field(default_factory=list)
    fuzzy_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    name_weight: float = Field(default=0.65, gt=0.0, lt=1.0)
    max_block_size: int = Field(default=2000, ge=2)
    # Columns counted for the completeness score; default: every non-internal staging column.
    completeness_columns: Optional[list[str]] = None

    @model_validator(mode="after")
    def _needs_something(self) -> DedupConfig:
        if not self.exact_keys and not self.name_column:
            raise ValueError("configure at least one exact key or a fuzzy name_column")
        return self


class ClusterSample(_Model):
    cluster_id: int
    master_pk: str
    member_pks: list[str]
    match_type: str


class DedupReport(_Model):
    input_rows: int
    eligible_rows: int
    excluded_failed_rows: int
    duplicate_clusters: int
    duplicate_children: int
    surviving_records: int  # eligible rows - duplicate children (+ excluded rows are not counted)
    exact_match_clusters: int
    fuzzy_match_clusters: int
    exact_edges_by_key: dict[str, int]
    fuzzy_pairs_evaluated: int
    fuzzy_pairs_matched: int
    samples: list[ClusterSample]
    output_path: str


# ------------------------------------------------------------------ run orchestration
class RunReport(_Model):
    run_id: int
    mapping_set_id: int
    wave_name: str
    status: str
    source_records: int
    transformed_records: int
    failed_records: int
    lineage_rows: int
    started_at: datetime
    completed_at: datetime
    staging_path: str
    deduplicated_path: Optional[str] = None
    transformation: TransformationResult
    deduplication: Optional[DedupReport] = None
    report_path: Optional[str] = None
    message: Optional[str] = None
