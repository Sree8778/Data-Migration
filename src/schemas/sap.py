"""Pydantic v2 models for Module 6: BP records, SAP returns, cockpit, loading, reconciliation."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ business partner record
class BusinessPartnerRecord(_Model):
    """One legacy customer assembled from a valid staging row, ready for cockpit or API load."""

    source_pk: str
    bp_ext: Optional[str] = None  # ALPHA-converted legacy key; the join key of every cockpit sheet
    general: dict[str, str] = Field(default_factory=dict)  # BUT000
    roles: list[str] = Field(default_factory=list)  # BUT100
    address: dict[str, str] = Field(default_factory=dict)  # ADRC
    company_code: dict[str, str] = Field(default_factory=dict)  # KNB1
    sales_area: dict[str, str] = Field(default_factory=dict)  # KNVV
    blocking_issues: list[str] = Field(default_factory=list)  # record cannot be migrated
    warnings: list[str] = Field(default_factory=list)  # data dropped / incomplete but non-blocking


# ------------------------------------------------------------------ SAP return structure
class BapiRet2(_Model):
    """Mirror of the standard SAP BAPIRET2 return structure."""

    TYPE: str = Field(pattern="^[SEWIA]$")  # Success, Error, Warning, Info, Abort
    ID: str = ""
    NUMBER: str = ""
    MESSAGE: str = ""

    @property
    def is_error(self) -> bool:
        return self.TYPE in ("E", "A")

    def text(self) -> str:
        code = f"{self.ID}/{self.NUMBER}" if self.ID else "-"
        return f"[{self.TYPE}] {code}: {self.MESSAGE}"


class SapRecordResult(_Model):
    source_pk: str
    partner: Optional[str] = None  # SAP Business Partner number when created
    messages: list[BapiRet2] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        return bool(self.partner) and not any(m.is_error for m in self.messages)

    def error_text(self) -> str:
        errors = [m.text() for m in self.messages if m.is_error]
        return " | ".join(errors) if errors else "no business partner number returned by SAP"


# ------------------------------------------------------------------ cockpit
class CockpitResult(_Model):
    path: str
    sheet_rows: dict[str, int]
    business_partners: int
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ loading
class LoadResult(_Model):
    run_id: int
    status: str
    attempted: int
    loaded: int
    failed: int
    batches: int
    batch_size: int
    error_samples: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime


# ------------------------------------------------------------------ reconciliation
class ReconCounts(_Model):
    extracted: int
    transformed_ok: int
    transform_failed: int
    duplicate_children: Optional[int] = None
    validation_rejected: Optional[int] = None
    valid: Optional[int] = None
    load_attempted: int
    loaded: int
    load_failed: int
    not_attempted: Optional[int] = None


class DropOffLine(_Model):
    reason: str
    count: int
    explanation: str
    sample_pks: list[str] = Field(default_factory=list)


class CheckResult(_Model):
    name: str
    passed: bool
    detail: str


class LineageSampleCheck(_Model):
    sampled: int
    passed: int
    failures: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class ReconciliationReport(_Model):
    run_id: int
    wave_name: str
    mapping_set_id: int
    mapping_version: int
    mapping_created_by: str
    mapping_approved_by: Optional[str]
    mapping_approved_at: Optional[datetime]
    run_status: str
    run_phase: str
    counts: ReconCounts
    dropoff: list[DropOffLine]
    accounted_rows: int
    variance: int  # extracted - accounted_rows; 0 means every source row is explained
    balanced: bool
    complete: bool  # False when no validation report was supplied (valid/duplicate split unknown)
    checks: list[CheckResult]
    lineage_sample: LineageSampleCheck
    validation_failures: dict[str, int] = Field(default_factory=dict)
    load_errors: dict[str, int] = Field(default_factory=dict)
    open_critical_defects: int
    generated_at: datetime

    @property
    def passed(self) -> bool:
        return self.balanced and all(c.passed for c in self.checks) and not self.lineage_sample.failures


class ExportFormat(str, enum.Enum):
    MARKDOWN = "markdown"
    HTML = "html"
