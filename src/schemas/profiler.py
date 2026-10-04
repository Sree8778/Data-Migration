"""Pydantic v2 result models for the deterministic profiler (Module 2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopValue(_Model):
    value: str
    count: int = Field(ge=0)


class PatternConformity(_Model):
    """How many non-null values fully match a pattern."""

    matched: int = Field(ge=0)
    checked: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)


class ColumnProfile(_Model):
    column_name: str
    position: int
    detected_type: str
    total_rows: int
    null_count: int
    null_ratio: float = Field(ge=0, le=1)
    distinct_count: int
    # distinct_count / non-null count: 1.0 means every populated value is unique.
    distinct_ratio: float = Field(ge=0, le=1)
    min_length: int | None
    max_length: int | None
    top_values: list[TopValue]
    sample_values: list[str]
    conformity: dict[str, PatternConformity]
    # True when the column is detected as numeric but contains values with leading zeros
    # (e.g. postal codes), i.e. loading it as a number would corrupt data.
    leading_zero_risk: bool


class KeyDuplicateReport(_Model):
    columns: list[str]
    duplicate_group_count: int
    duplicate_row_count: int  # surplus rows: rows beyond the first of each group
    sample_duplicate_keys: list[list[str]]


class TableProfileResult(_Model):
    source_path: str
    file_format: str
    total_rows: int
    column_count: int
    columns: list[ColumnProfile]
    full_row_duplicate_rows: int
    full_row_duplicate_ratio: float
    key_duplicates: list[KeyDuplicateReport]
    profiled_at: datetime
    profiler_version: str


class SaveProfileSummary(_Model):
    table_id: int
    row_count_estimate: int
    columns_updated: int
    unmatched_profile_columns: list[str]
    catalog_columns_not_profiled: list[str]
    masked_sample_columns: list[str]
