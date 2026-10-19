"""Pydantic v2 models for Data Construction (missing-data remediation through Excel)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.validation import DefectRecordSummary, ReadinessResult, ValidationReport


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConstructionField(_Model):
    header: str  # column header in the workbook
    staging_column: str  # e.g. KNB1_AKONT
    target: str  # TABLE.COLUMN
    field_rule_id: int
    data_type: str
    max_length: Optional[int] = None
    check_table: Optional[str] = None
    mandatory: bool = False
    records_needing: int = 0


class TemplateResult(_Model):
    run_id: int
    template_id: str
    path: str
    records: int  # rows in the workbook
    fields: list[ConstructionField]
    pending_cells: int
    truncated: bool  # more records need construction than fit (download again after ingesting)
    total_records_needing: int
    generated_at: datetime


class RejectedCell(_Model):
    row_ref: int
    source_pk: str
    field: str
    value: str
    reason: str


class ConstructionResult(_Model):
    run_id: int
    records_updated: int
    cells_modified: int
    cells_rejected: int
    cells_ignored: int  # blank, already filled, or not requested for that record
    validation_status: Literal["OK", "PARTIAL", "REJECTED"]
    rejected_samples: list[RejectedCell] = Field(default_factory=list)
    lineage_rows: int
    remaining_cells_pending: int
    revalidation_required: bool
    warnings: list[str] = Field(default_factory=list)
    uploaded_by: str
    uploaded_at: datetime


class RevalidationResult(_Model):
    run_id: int
    validation: ValidationReport
    defects: DefectRecordSummary
    readiness: ReadinessResult
    signoff_revoked: bool
    deduplicated_again: bool


class RequiredField(_Model):
    target: str
    header: str
    records: int
    kind: str


class ActivityEntry(_Model):
    event: Literal["template", "upload", "revalidate"]
    at: datetime
    user: str
    detail: dict = Field(default_factory=dict)


class ConstructionSummary(_Model):
    run_id: int
    status: Literal["NOT_REQUIRED", "PENDING", "REVALIDATION_REQUIRED", "LOCKED"]
    can_construct: bool
    reason: Optional[str] = None
    incomplete_records: int
    required_fields: list[RequiredField]
    pending_cells: int
    activity: list[ActivityEntry]


class PlaceholderRule(_Model):
    rule_id: int
    target: str
