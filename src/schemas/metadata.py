"""Pydantic v2 payloads for the metadata service (inputs and outputs)."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.db.enums import MappingStatus, PiiClassification, SystemType, TargetType

_SECRET_KEY = re.compile(r"(password|passwd|pwd|secret|token|api_?key)", re.IGNORECASE)


class _InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ data sources
class DataSourceCreate(_InputModel):
    system_name: str = Field(min_length=1, max_length=128)
    system_type: SystemType
    target_type: TargetType
    connection_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("connection_config")
    @classmethod
    def _no_plaintext_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Governance guard: only references to secrets (keys ending in `_ref`) are allowed."""
        for key in value:
            if _SECRET_KEY.search(key) and not key.lower().endswith("_ref"):
                raise ValueError(
                    f"connection_config key '{key}' looks like a plaintext secret; "
                    f"store a reference instead (e.g. '{key}_ref')"
                )
        return value


class DataSourceRead(_OutputModel):
    id: int
    system_name: str
    system_type: SystemType
    target_type: TargetType
    connection_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ tables
class TableCreate(_InputModel):
    source_id: int
    table_name: str = Field(min_length=1, max_length=128)
    business_name: str | None = Field(default=None, max_length=256)
    description: str | None = None
    row_count_estimate: int | None = Field(default=None, ge=0)
    business_object: str | None = Field(default=None, max_length=64)


class TableRead(_OutputModel):
    id: int
    source_id: int
    table_name: str
    business_name: str | None
    description: str | None
    row_count_estimate: int | None
    last_profiled_at: datetime | None
    business_object: str | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ columns
class ColumnCreate(_InputModel):
    column_name: str = Field(min_length=1, max_length=128)
    data_type: str = Field(min_length=1, max_length=64)
    char_max_length: int | None = Field(default=None, gt=0)
    numeric_precision: int | None = Field(default=None, gt=0)
    numeric_scale: int | None = Field(default=None, ge=0)
    is_nullable: bool = True
    is_primary_key: bool = False
    pii_classification: PiiClassification = PiiClassification.INTERNAL
    sample_values: list[Any] | None = None
    null_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    distinct_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    ordinal_position: int | None = Field(default=None, ge=0)
    is_mandatory: bool = False
    check_table: str | None = Field(default=None, max_length=64)
    description: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ColumnCreate:
        if self.numeric_scale is not None:
            if self.numeric_precision is None or self.numeric_scale > self.numeric_precision:
                raise ValueError("numeric_scale requires numeric_precision >= numeric_scale")
        if self.is_primary_key and self.is_nullable:
            # Primary keys are implicitly NOT NULL; normalise instead of rejecting.
            self.is_nullable = False
        return self


class ColumnRead(_OutputModel):
    id: int
    table_id: int
    column_name: str
    data_type: str
    char_max_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    is_nullable: bool
    is_primary_key: bool
    pii_classification: PiiClassification
    sample_values: list[Any] | None
    null_ratio: Decimal | None
    distinct_ratio: Decimal | None
    ordinal_position: int
    is_mandatory: bool
    check_table: str | None
    description: str | None


class BulkColumnRegistration(_InputModel):
    table_id: int
    columns: list[ColumnCreate] = Field(min_length=1)

    @field_validator("columns")
    @classmethod
    def _unique_names(cls, value: list[ColumnCreate]) -> list[ColumnCreate]:
        names = [c.column_name for c in value]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate column_name(s) in payload: {dupes}")
        return value


# ------------------------------------------------------------------ target dictionary
class TargetTableDictionary(_OutputModel):
    table: TableRead
    columns: list[ColumnRead]


class TargetDictionary(_OutputModel):
    object_name: str
    tables: list[TargetTableDictionary]


# ------------------------------------------------------------------ mapping sets
class MappingSetCreate(_InputModel):
    source_table_id: int
    target_table_id: int
    created_by: str = Field(min_length=1, max_length=128)
    version: int | None = Field(default=None, ge=1, description="Defaults to the next free version")


class MappingSetRead(_OutputModel):
    id: int
    source_table_id: int
    target_table_id: int
    version: int
    status: MappingStatus
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
