"""Pydantic v2 models for the source landing framework (multi-format file sources)."""

from __future__ import annotations

import codecs
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceFormat = Literal["delimited", "excel", "json", "fixed", "parquet"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class FixedColumn(_Model):
    name: str = Field(min_length=1, max_length=128)
    start: int = Field(ge=1, description="1-based position of the first character")
    width: int = Field(ge=1, le=100_000)


class LandingOptions(_Model):
    """How to read a source file. Everything is optional: omitted values are detected."""

    format: Optional[SourceFormat] = None
    encoding: Optional[str] = None
    delimiter: Optional[str] = Field(default=None, min_length=1, max_length=1)
    quote: str = Field(default='"', max_length=1)
    header: bool = True
    skip_rows: int = Field(default=0, ge=0, le=100_000)
    sheet: Optional[str] = Field(default=None, max_length=128)
    header_row: int = Field(default=1, ge=1, le=100_000, description="Excel: row holding the column names")
    member: Optional[str] = Field(default=None, max_length=512, description="file inside a .zip")
    columns: Optional[list[FixedColumn]] = Field(default=None, description="required for fixed-width files")
    null_tokens: list[str] = Field(default_factory=list, description="extra values read as NULL, e.g. NULL, \\N")
    expected_rows: Optional[int] = Field(default=None, ge=0, description="control total from the source system")
    tolerate_rejects: bool = False

    @field_validator("encoding")
    @classmethod
    def _known_encoding(cls, value: Optional[str]) -> Optional[str]:
        if value:
            try:
                codecs.lookup(value)
            except LookupError:
                raise ValueError(f"unknown encoding '{value}'") from None
        return value


class SourcePreview(_Model):
    format: SourceFormat
    encoding: Optional[str] = None
    encoding_basis: Optional[str] = None  # how it was decided: BOM / valid UTF-8 / fallback
    delimiter: Optional[str] = None
    sheets: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    columns: list[str]
    rows: list[list[Optional[str]]]
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    options: LandingOptions  # the resolved options the preview was produced with


class ColumnInfo(_Model):
    name: str
    detected_type: str


class SnapshotInfo(_Model):
    format: SourceFormat
    original_filename: str
    original_sha256: str
    original_bytes: int
    rows_landed: int
    rejected_rows: int = 0
    expected_rows: Optional[int] = None
    control_status: Literal["NOT_PROVIDED", "MATCH"] = "NOT_PROVIDED"  # a mismatch aborts the landing
    columns: list[ColumnInfo]
    options: LandingOptions
    detection: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    origin: dict[str, Any] = Field(default_factory=dict)  # where the data came from when it was not an uploaded file (database, schema, table...)
    landed_at: datetime
    landed_by: str

    @property
    def detected_types(self) -> dict[str, str]:
        return {c.name: c.detected_type for c in self.columns}
