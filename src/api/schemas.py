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
    format: Optional[str] = None
    rows_landed: Optional[int] = None
    control_status: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    origin: Optional[dict[str, Any]] = None
    extract_seconds: Optional[float] = None


Kind = Literal["postgresql", "oracle", "mssql", "mysql", "azure_blob", "s3", "sftp"]


class ConnectionIn(_In):
    name: str = Field(min_length=1, max_length=64)
    dialect: Kind
    host: str = Field(min_length=1, max_length=253)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    database: str = Field(default="", max_length=128, description="database name; for Oracle the service name")
    user: str = Field(min_length=1, max_length=128)
    password: Optional[str] = Field(default=None, max_length=512, repr=False, description="kept in server memory only, never stored")
    password_env: Optional[str] = Field(default=None, max_length=64, description="name of a server environment variable that holds the password")
    options: dict[str, Any] = Field(default_factory=dict)
    role: Literal["source", "target"] = Field(default="source", description="target = the application may create tables and load into this database")
    default_schema: Optional[str] = Field(default=None, max_length=128, description="target: the schema new tables are created in")


class ConnectionTestIn(_In):
    name: Optional[str] = Field(default=None, description="test a saved connection (optionally with a fresh password)")
    dialect: Optional[Kind] = None
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    database: str = ""
    user: Optional[str] = None
    password: Optional[str] = Field(default=None, max_length=512, repr=False)
    password_env: Optional[str] = Field(default=None, max_length=64, description="name of a server environment variable that holds the password")
    options: dict[str, Any] = Field(default_factory=dict)


class ConnectionOut(BaseModel):
    name: str
    dialect: str
    label: str
    host: str
    port: int
    database: str
    user: str
    password_source: str          # memory | env:NAME | missing - never the password
    options: dict[str, Any]
    role: str = "source"
    default_schema: Optional[str] = None
    created_by: str
    updated_at: str


class DialectInfo(BaseModel):
    dialect: str
    label: str
    default_port: int
    database_label: str
    family: str = "database"       # database | storage


class PresetInfo(BaseModel):
    id: str
    label: str
    dialect: str
    port: int
    options: dict[str, Any]
    hint: str


class ConnectionTestResult(BaseModel):
    ok: bool
    detail: str
    server_version: Optional[str] = None
    latency_ms: int = 0
    schemas: Optional[int] = None      # schemas visible (databases) or containers / buckets visible (storage)
    host_key: Optional[str] = None     # SFTP: the key the server presented when it is not yet trusted


class WriteTestIn(_In):
    schema_name: str = Field(min_length=1, max_length=128)


class DbLoadRequest(_In):
    connection: str
    schema_name: Optional[str] = Field(default=None, max_length=128, description="default: the target connection's default schema")
    table_name: str = Field(min_length=1, max_length=30)
    mode: Literal["create", "replace"] = Field(default="create", description="create fails if the table exists; replace drops it first")
    include_source_columns: bool = Field(default=False, description="also deliver the raw legacy values (SRC_*)")
    batch_size: int = Field(default=1000, ge=1, le=10000)


class DbLoadPreviewColumn(BaseModel):
    name: str
    type: str
    nullable: bool


class DbLoadPreview(BaseModel):
    run_id: int
    connection: str
    schema_name: str
    table: str
    rows: int
    columns: list[DbLoadPreviewColumn]
    ddl: str


class DbLoadResult(BaseModel):
    run_id: int
    connection: str
    dialect: str
    host: str
    port: int
    database: str
    schema_name: str
    table: str
    mode: str
    columns: int
    include_source_columns: bool
    valid_records: int
    rows_loaded: int
    rows_in_target: int
    replaced_existing: bool
    seconds: float
    loaded_by: str
    loaded_at: str
    ddl: str


class TargetKind(BaseModel):
    kind: Literal["sandbox", "cockpit", "live"]
    label: str
    available: bool
    simulated: bool
    testable: bool
    detail: str


class TargetsResponse(BaseModel):
    object_name: str
    catalog_ready: bool
    targets: list[TargetKind]
    databases: list[ConnectionOut] = []   # saved database TARGET connections (Oracle, SQL Server, PostgreSQL, MySQL)


class TargetTestRequest(_In):
    target: Literal["sandbox", "live"]


class TargetTestResult(BaseModel):
    target: str
    ok: bool
    detail: str
    latency_ms: int


class StorageFile(BaseModel):
    name: str
    key: str
    size: Optional[int] = None
    modified: Optional[str] = None
    is_dir: bool


class FileImportRequest(_In):
    connection: str
    container: str = Field(min_length=1, max_length=256)
    key: str = Field(min_length=1, max_length=1024)
    system_name: Optional[str] = Field(default=None, max_length=128, description="default: the connection name")
    table_name: Optional[str] = Field(default=None, max_length=128, description="default: derived from the file name")
    business_name: Optional[str] = Field(default=None, max_length=256)
    primary_key_columns: Optional[list[str]] = None
    options: Optional[dict[str, Any]] = Field(default=None, description="LandingOptions (format, encoding, delimiter, sheet...); omitted values are detected")
    expected_rows: Optional[int] = Field(default=None, ge=0, description="control total from the source system")


class DbTable(BaseModel):
    schema_name: str
    name: str
    kind: str
    row_estimate: Optional[int] = None


class DbColumn(BaseModel):
    name: str
    data_type: str
    length: Optional[int]
    nullable: bool
    is_primary_key: bool
    position: int


class DbPreview(BaseModel):
    columns: list[DbColumn]
    rows: list[list[str]]


class DbImportRequest(_In):
    connection: str
    schema_name: str = Field(min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    columns: Optional[list[str]] = Field(default=None, description="subset of columns; default all")
    system_name: Optional[str] = Field(default=None, max_length=128, description="default: the connection name")
    table_name: Optional[str] = Field(default=None, max_length=128, description="default: the source table name")
    business_name: Optional[str] = Field(default=None, max_length=256)
    primary_key_columns: Optional[list[str]] = Field(default=None, description="default: the primary key defined in the database")
    control_total: bool = Field(default=True, description="reject the extract unless the rows read equal the source row count taken in the same transaction")


class TableSummary(BaseModel):
    table_id: int
    table_name: str
    system_name: str
    system_type: str
    row_count_estimate: Optional[int]
    last_profiled_at: Optional[datetime]
    has_source_file: bool
    column_count: int
    source_format: Optional[str] = None
    rows_landed: Optional[int] = None


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
    archived: bool = False
    archive_reason: Optional[str] = None
    archived_by: Optional[str] = None


class ArchiveRequest(_In):
    reason: str = Field(min_length=3, max_length=300)


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
    missing_from_file: bool = False  # still in the catalog (a rule uses it) but not in the landed file
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


class JobProgress(BaseModel):
    current: int  # columns completed so far
    total: int
    column: Optional[str] = None  # column the model is evaluating right now
    message: str


class JobError(BaseModel):
    code: str
    detail: Any


class JobStatus(BaseModel):
    job_id: str
    kind: str
    source_table_id: int
    started_by: str
    status: Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]
    created_at: datetime
    finished_at: Optional[datetime] = None
    progress: JobProgress
    result: Optional[GenerateResponse] = None  # set when status == succeeded
    error: Optional[JobError] = None
    already_running: bool = False


class MappingSetCreateRequest(_In):
    source_table_id: int
    target_object: str = Field(default="BUSINESS_PARTNER", min_length=1, max_length=64)


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


class KeyAdd(_In):
    api_key: str = Field(min_length=16, max_length=256)
    label: str = Field(default="", max_length=60)


class KeyTest(_In):
    api_key: Optional[str] = Field(default=None, min_length=16, max_length=256)
    key_hash: Optional[str] = Field(default=None, min_length=4, max_length=64)


class KeyInfo(BaseModel):
    key_hash: str
    masked_key: str
    label: str
    source: str
    status: Literal["ACTIVE", "RATE_LIMITED", "QUOTA_EXHAUSTED", "INVALID"]
    available_at: Optional[datetime]
    seconds_until_available: int
    request_count: int
    success_count: int
    error_count: int
    rate_limit_count: int
    last_error: Optional[str]
    last_used_at: Optional[datetime]
    added_at: datetime


class KeyPoolResponse(BaseModel):
    provider: str
    model: str
    total: int
    active: int
    cooling_down: int
    quota_exhausted: int
    invalid: int
    persisted: bool
    keys: list[KeyInfo]


class KeyTestResult(BaseModel):
    ok: bool
    classification: str
    status_code: Optional[int]
    detail: str
    latency_ms: int
    model: str


class HealthResponse(BaseModel):
    status: str
    database: str
    auth_mode: str
    jira_mode: str
    sap_default_target: str
    workspace: str
    target_catalog_ready: bool
    llm_provider: str = "ollama"
    gemini_keys_active: int = 0
    severity_levels: list[str] = [s.value for s in Severity]
