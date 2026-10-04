"""Request / response models of the REST API (Pydantic v2). Domain models are reused from the
module schemas where they already fit."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.execution import DedupConfig, DedupReport, RunReport
from src.schemas.mapping_ai import MappingGenerationReport
from src.schemas.profiler import KeyDuplicateReport, TopValue
from src.schemas.sap import CockpitResult, LoadResult, ReconciliationReport
from src.schemas.validation import (
    DefectRead,
    DefectRecordSummary,
    ReadinessResult,
    Severity,
    ValidationReport,
)

RuleTypeName = Literal["DIRECT_COPY", "VALUE_MAPPING", "SQL_EXPRESSION", "STATIC_VALUE"]
Band = Literal["high", "medium", "low"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApiError(BaseModel):
    detail: Any
    code: str


# ------------------------------------------------------------------ sources / tables
class SourceRegistration(BaseModel):
    source_id: int
    system_name: str
    table_id: Optional[int] = None
    table_name: Optional[str] = None
    columns_registered: int = 0
    filename: Optional[str] = None
    bytes_stored: int = 0


class TableSummary(BaseModel):
    table_id: int
    table_name: str
    system_name: str
    system_type: str
    row_count_estimate: Optional[int]
    last_profiled_at: Optional[datetime]
    has_source_file: bool
    column_count: int


# ------------------------------------------------------------------ profiling
class ColumnHealth(BaseModel):
    column_name: str
    position: int
    detected_type: str
    null_count: int
    null_ratio: float
    distinct_count: int
    distinct_ratio: float
    min_length: Optional[int]
    max_length: Optional[int]
    top_values: list[TopValue]
    sample_values: list[str]
    pii_masked: bool
    health_score: float = Field(ge=0, le=100)
    health_band: Band
    flags: list[str]


class ProfileResponse(BaseModel):
    table_id: int
    table_name: str
    profiled_at: datetime
    total_rows: int
    column_count: int
    average_health_score: float
    full_row_duplicate_rows: int
    full_row_duplicate_ratio: float
    key_duplicates: list[KeyDuplicateReport]
    inferred_keys: list[list[str]]
    columns: list[ColumnHealth]


# ------------------------------------------------------------------ mapping
class MappingSetSummary(BaseModel):
    mapping_set_id: int
    source_table_id: int
    target_table: str
    version: int
    status: str
    created_by: str
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    rule_count: int


class RuleDetail(BaseModel):
    rule_id: int
    mapping_set_id: int
    source_column: Optional[str]
    target_table: str
    target_column: str
    target_data_type: str
    target_length: Optional[int]
    target_mandatory: bool
    rule_type: str
    transformation_logic: Optional[str]
    ai_suggested: bool
    ai_model_name: Optional[str]
    confidence: Optional[float]
    confidence_band: Optional[Band]
    reasoning: Optional[str]  # cleaned (no review prefix / reviewer suffix)
    needs_review: bool
    review_reasons: list[str]
    reviewed_by: Optional[str]
    lookup_count: int


class SourceColumnView(BaseModel):
    column_name: str
    data_type: str
    null_ratio: Optional[float]
    distinct_ratio: Optional[float]
    sample_values: list[str]
    mapped: bool


class MappingDetail(BaseModel):
    mapping: MappingSetSummary
    rules: list[RuleDetail]
    source_columns: list[SourceColumnView]
    unmapped_mandatory_targets: list[str]


class GenerateResponse(BaseModel):
    mapping_set_id: int
    report: MappingGenerationReport
    mapping: MappingDetail


class RuleUpdate(_In):
    rule_type: Optional[RuleTypeName] = None
    transformation_logic: Optional[str] = Field(default=None, max_length=1000)
    source_column: Optional[str] = None
    target_table: Optional[str] = None
    target_column: Optional[str] = None
    lookups: Optional[dict[str, str]] = None
    confirm: bool = False  # human accepts the rule: clears the NEEDS_REVIEW flag
    note: Optional[str] = Field(default=None, max_length=300)


class RuleCreate(_In):
    target_table: str = Field(min_length=1)
    target_column: str = Field(min_length=1)
    rule_type: RuleTypeName
    source_column: Optional[str] = None
    transformation_logic: Optional[str] = Field(default=None, max_length=1000)
    lookups: Optional[dict[str, str]] = None
    note: Optional[str] = Field(default=None, max_length=300)


# ------------------------------------------------------------------ pipeline
class PipelineRunRequest(_In):
    wave_name: str = Field(default="WAVE_1", min_length=1, max_length=128)
    deduplicate: bool = True
    dedup: Optional[DedupConfig] = None  # default: fuzzy name/address on the mapped BP fields
    passthrough_columns: list[str] = Field(default_factory=list)
    lookup_fallback: Literal["FLAG_FAILED", "PASS_THROUGH", "USE_DEFAULT"] = "FLAG_FAILED"
    lookup_default: Optional[str] = None
    lineage: Literal["ALL", "EXCEPTIONS", "NONE"] = "ALL"


class PipelineRunResponse(BaseModel):
    run_id: int
    run: RunReport
    validation: ValidationReport
    defects: DefectRecordSummary
    readiness: ReadinessResult
    dedup_applied: bool
    dedup: Optional[DedupReport] = None


class RunSummary(BaseModel):
    run_id: int
    mapping_set_id: int
    wave_name: str
    status: str
    phase: str
    records_extracted: int
    records_transformed: int
    records_loaded: int
    records_failed: int
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    quality_score: Optional[float] = None


# ------------------------------------------------------------------ defects / governance
class DefectDetail(DefectRead):
    target: str
    root_cause: str


class DefectsResponse(BaseModel):
    run_id: int
    quality_score: Optional[float]
    open_defects: int
    open_critical: int
    jira_mode: Literal["live", "dry-run"]
    defects: list[DefectDetail]
    readiness: ReadinessResult


class JiraSyncResponse(BaseModel):
    run_id: int
    dry_run: bool
    issue_keys: list[str]
    errors: list[str]


class SignOffResponse(BaseModel):
    run_id: int
    signed_off_by: str
    signed_off_at: datetime
    readiness: ReadinessResult


class LoadRequest(_In):
    mode: Literal["cockpit", "sap"] = "cockpit"
    target: Literal["sandbox", "live"] = "sandbox"
    batch_size: int = Field(default=100, ge=1, le=1000)


class LoadResponse(BaseModel):
    run_id: int
    mode: str
    simulated: bool
    load: Optional[LoadResult] = None
    cockpit: Optional[CockpitResult] = None
    download_url: Optional[str] = None


class ReconciliationResponse(BaseModel):
    report: ReconciliationReport
    passed: bool
    cockpit_download_url: Optional[str]
    exports: dict[str, str]
    export_urls: dict[str, str]


class HealthResponse(BaseModel):
    status: str
    database: str
    auth_mode: str
    jira_mode: str
    sap_default_target: str
    workspace: str
    target_catalog_ready: bool
    severity_levels: list[str] = [s.value for s in Severity]
